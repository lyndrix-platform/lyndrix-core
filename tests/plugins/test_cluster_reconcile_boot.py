"""A fresh cluster instance converges on plugin_states at boot (not five minutes
later), release tags are tried in both spellings, and the post-install gate
accepts a plugin that is pinned to another role.

Found while migrating the production database into the k3s cluster: a new pod
with an empty plugin directory served 404s for every plugin route until the
first periodic reconcile; rows recorded "1.1.2" while the repo only tags
"v1.1.2"; the orchestrator (pinned:runner) was flagged "verification failed"
on an api-only instance because health() ran without setup().

Run inside the lyndrix-core runtime:
    pytest tests/plugins/test_cluster_reconcile_boot.py -v
"""
import asyncio
import types

import pytest

from config import settings
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.plugin_service import PluginService


# --- tag spelling fallback -----------------------------------------------------

@pytest.mark.parametrize(
    "version, expected",
    [
        ("1.1.2", ["v1.1.2"]),
        ("v1.1.2", ["1.1.2"]),
        ("V2.0.0", ["2.0.0"]),
        ("latest", []),
        ("", []),
        ("main", []),          # a branch name has no alternate spelling
        ("v", []),             # not a version
    ],
)
def test_alternate_tag_forms(version, expected):
    assert PluginService._alternate_tag_forms(version) == expected


# --- boot-time convergence ----------------------------------------------------

def _manager_with_recorders(monkeypatch):
    mgr = ModuleManager()
    calls = []

    async def fake_desired():
        calls.append("desired")

    async def fake_cluster(*, trigger="manual"):
        calls.append(f"cluster:{trigger}")

    monkeypatch.setattr(mgr, "reconcile_desired_plugins", fake_desired)
    monkeypatch.setattr(mgr, "reconcile_cluster_state", fake_cluster)
    return mgr, calls


def test_boot_converges_on_plugin_states_in_cluster_mode(monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "cluster")
    mgr, calls = _manager_with_recorders(monkeypatch)
    asyncio.run(mgr._converge_after_restore())
    # desired pins first (they may add rows), then the plugin_states pass
    assert calls == ["desired", "cluster:boot"]


def test_boot_skips_cluster_reconcile_in_single_mode(monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "single")
    mgr, calls = _manager_with_recorders(monkeypatch)
    asyncio.run(mgr._converge_after_restore())
    assert calls == ["desired"]


def test_boot_reconcile_failure_does_not_abort_startup(monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "cluster")
    mgr, calls = _manager_with_recorders(monkeypatch)

    async def boom(*, trigger="manual"):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(mgr, "reconcile_cluster_state", boom)
    asyncio.run(mgr._converge_after_restore())  # must not raise
    assert calls == ["desired"]


# --- post-install gate for remote (pinned elsewhere) plugins --------------------

def _entry(status, health_status="error"):
    class _Health:
        def __init__(self):
            self.status = health_status
            self.details = {"reason": "not_initialized"}

    module = types.SimpleNamespace(health=lambda ctx: _Health())
    manifest = types.SimpleNamespace(type="PLUGIN", version="1.3.0")
    return {"status": status, "module": module, "context": None, "manifest": manifest}


def test_verify_passes_for_remote_plugin_without_calling_health():
    mgr = ModuleManager()
    mgr.registry["lyndrix.plugin.pinned"] = _entry("remote")
    assert asyncio.run(mgr._verify_after_install("lyndrix.plugin.pinned")) is True


def test_verify_still_fails_on_health_error_for_active_plugin():
    mgr = ModuleManager()
    mgr.registry["lyndrix.plugin.local"] = _entry("active")
    assert asyncio.run(mgr._verify_after_install("lyndrix.plugin.local")) is False
