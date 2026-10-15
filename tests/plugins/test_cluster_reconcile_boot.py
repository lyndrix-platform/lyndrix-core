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


# --- version drift: a peer upgraded → this instance follows ----------------------

def _reconcile_env(monkeypatch, *, have: str, want: str, status: str = "remote"):
    from core.components.database.logic import db_service
    from core.components.plugins.logic import plugin_service as ps_mod

    mgr = ModuleManager()
    calls = []

    async def fake_install(url, version="latest", upgrade=False, notify=True):
        calls.append({"url": url, "version": version, "upgrade": upgrade, "notify": notify})
        return True

    monkeypatch.setattr(ps_mod.plugin_service, "install_plugin", fake_install)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    row = {"module_id": "lyndrix.plugin.x", "is_active": True, "installed_version": want,
           "desired_version": want, "repo_url": "https://github.com/lyndrix-platform/lyndrix-plugin-x"}
    monkeypatch.setattr(mgr, "_load_plugin_states", lambda: [row])
    manifest = types.SimpleNamespace(type="PLUGIN", version=have, id="lyndrix.plugin.x")
    mgr.registry["lyndrix.plugin.x"] = {"status": status, "manifest": manifest, "module": None,
                                        "context": None, "persisted": True}
    return mgr, calls


def test_peer_upgrade_is_followed(monkeypatch):
    mgr, calls = _reconcile_env(monkeypatch, have="1.3.0", want="1.4.0")
    summary = asyncio.run(mgr.reconcile_cluster_state(trigger="test"))
    assert calls == [{"url": "https://github.com/lyndrix-platform/lyndrix-plugin-x", "version": "1.4.0",
                      "upgrade": True, "notify": False}]
    assert summary.get("upgraded") == ["lyndrix.plugin.x"]


def test_same_version_in_either_spelling_is_left_alone(monkeypatch):
    mgr, calls = _reconcile_env(monkeypatch, have="1.4.0", want="v1.4.0", status="active")
    asyncio.run(mgr.reconcile_cluster_state(trigger="test"))
    assert calls == []


# --- post-install gate tolerates a plugin that is still bootstrapping ---------------

def test_verify_waits_for_a_bootstrapping_plugin(monkeypatch):
    mgr = ModuleManager()
    monkeypatch.setattr(ModuleManager, "_VERIFY_HEALTH_PAUSE_S", 0.01)
    calls = {"n": 0}

    class _Health:
        def __init__(self, status):
            self.status = status
            self.details = {"reason": "tables_not_initialized"}

    def health(ctx):
        calls["n"] += 1
        return _Health("error" if calls["n"] < 3 else "ok")  # ready on the third check

    module = types.SimpleNamespace(health=health)
    mgr.registry["lyndrix.plugin.boot"] = {"status": "active", "module": module, "context": None,
                                           "manifest": types.SimpleNamespace(type="PLUGIN", version="0.2.0")}
    assert asyncio.run(mgr._verify_after_install("lyndrix.plugin.boot")) is True
    assert calls["n"] == 3


def test_verify_still_fails_when_error_persists(monkeypatch):
    mgr = ModuleManager()
    monkeypatch.setattr(ModuleManager, "_VERIFY_HEALTH_PAUSE_S", 0.01)
    monkeypatch.setattr(ModuleManager, "_VERIFY_HEALTH_ATTEMPTS", 3)
    mgr.registry["lyndrix.plugin.bad"] = _entry("active")  # health() always "error"
    assert asyncio.run(mgr._verify_after_install("lyndrix.plugin.bad")) is False


# --- a local revert must not move plugin_states (the cluster's version) ---------------

def test_persist_state_keeps_row_version_after_a_local_revert(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from core.components.database.logic import db_service
    from core.components.plugins.logic.models import PluginState

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginState.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    with Session() as s:
        s.add(PluginState(module_id="lyndrix.plugin.r", is_active=True, installed_version="0.2.0",
                          desired_version="0.2.0", repo_url="https://x/y"))
        s.commit()

    mgr = ModuleManager()
    monkeypatch.setattr(mgr, "_ensure_plugin_state_schema", lambda: None)
    manifest = types.SimpleNamespace(type="PLUGIN", version="0.1.0", id="lyndrix.plugin.r", repo_url="https://x/y")
    mgr.registry["lyndrix.plugin.r"] = {"status": "active", "manifest": manifest, "module": None, "context": None,
                                        "reverted_from": "0.2.0"}
    mgr._persist_plugin_state("lyndrix.plugin.r", True)
    with Session() as s:
        row = s.get(PluginState, "lyndrix.plugin.r")
        assert row.installed_version == "0.2.0"          # the cluster still wants 0.2.0
    # without the flag the same call records the loaded version
    mgr.registry["lyndrix.plugin.r"].pop("reverted_from")
    mgr._persist_plugin_state("lyndrix.plugin.r", True)
    with Session() as s:
        assert s.get(PluginState, "lyndrix.plugin.r").installed_version == "0.1.0"
