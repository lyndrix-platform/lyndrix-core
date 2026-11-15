"""CL-02 (ModuleManager side): ``module_manager.boot_converged``.

The cluster readiness check reads it via ``getattr(module_manager,
"boot_converged", True)``: False from process start, True once the first
activation pass (saved states, desired plugins, cluster reconcile) is through
and nothing it started is still installing or setting up.
"""
import asyncio
import types

import pytest

import core.services  # noqa: F401  (import order: services before api)
from config import settings
from core.components.plugins.logic import manager as manager_mod
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions


def _entry(mid: str, setup) -> dict:
    manifest = ModuleManifest(id=mid, name=mid, version="1.0.0", type="PLUGIN",
                              permissions=ModulePermissions(emit=[], subscribe=[]))
    module = types.SimpleNamespace(setup=setup, __file__=f"/nonexistent/{mid}/entrypoint.py",
                                   __name__=f"plugins.{mid.replace('.', '_')}.entrypoint")
    return {"manifest": manifest, "module": module, "context": ModuleContext(manifest),
            "status": "initializing", "error_message": None}


@pytest.fixture()
def mgr(monkeypatch):
    m = ModuleManager()
    m.row_states = {}

    async def _no_desired():
        return None

    monkeypatch.setattr(manager_mod.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(m, "_restore_row_states", lambda loaded: dict(m.row_states))
    monkeypatch.setattr(m, "reconcile_desired_plugins", _no_desired)
    monkeypatch.setattr(m, "_mount_plugin_statics", lambda mid: None)
    return m


async def _settle(ticks: int = 5):
    for _ in range(ticks):
        await asyncio.sleep(0.01)


def test_false_from_process_start_and_a_plain_bool():
    mgr = ModuleManager()
    assert mgr.boot_converged is False
    assert isinstance(manager_mod.module_manager.boot_converged, bool)


def test_single_mode_converges_after_the_first_activation_pass(mgr, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "single")
    mgr.registry["p.sync"] = _entry("p.sync", lambda ctx: None)
    mgr.row_states = {"p.sync": True}

    async def run():
        await mgr._activate_saved_plugins()
        await _settle()

    asyncio.run(run())
    assert mgr.boot_converged is True


def test_waits_for_the_boot_reconcile_in_cluster_mode(mgr, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "cluster")
    seen = []

    async def run():
        gate = asyncio.Event()

        async def slow_reconcile(*, trigger="manual"):
            seen.append(trigger)
            await gate.wait()                       # downloading + pip-installing plugins
            return {}

        monkeypatch.setattr(mgr, "reconcile_cluster_state", slow_reconcile)
        boot = asyncio.ensure_future(mgr._activate_saved_plugins())
        await _settle()
        assert mgr.boot_converged is False          # reconcile still running
        gate.set()
        await boot
        await _settle()
        assert mgr.boot_converged is True

    asyncio.run(run())
    assert seen == ["boot"]


def test_waits_for_an_async_setup_and_for_installs_in_flight(mgr, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "single")

    async def run():
        gate = asyncio.Event()

        async def setup(ctx):
            await gate.wait()

        mgr.registry["p.async"] = _entry("p.async", setup)
        mgr.row_states = {"p.async": True}
        install_lock = mgr._lock_for("install:some_plugin")
        await install_lock.acquire()                # the load handler of a boot install

        await mgr._activate_saved_plugins()
        mgr._boot_watch_task.cancel()
        watch = asyncio.ensure_future(mgr._watch_boot_convergence(poll_s=0.01))
        await _settle()
        assert mgr.boot_converged is False          # async setup + install pending

        gate.set()
        await _settle()
        assert mgr.registry["p.async"]["status"] == "active"
        assert mgr.boot_converged is False          # the install handler still holds its lock

        install_lock.release()
        await watch
        assert mgr.boot_converged is True

    asyncio.run(run())


def test_backed_off_and_failed_plugins_count_as_converged(mgr, monkeypatch):
    """A dead repo or a broken plugin must not keep the instance unready forever."""
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "cluster")

    def broken_setup(ctx):
        raise RuntimeError("boom")

    async def reconcile(*, trigger="manual"):
        mgr._reconcile_install_failed("lyndrix.plugin.dead_repo")     # install went into back-off
        return {"installed": []}

    monkeypatch.setattr(mgr, "reconcile_cluster_state", reconcile)
    mgr.registry["p.broken"] = _entry("p.broken", broken_setup)
    mgr.row_states = {"p.broken": True}

    async def run():
        await mgr._activate_saved_plugins()
        await _settle()

    asyncio.run(run())
    assert mgr.registry["p.broken"]["status"] == "failed"
    assert mgr.boot_converged is True


def test_a_failed_boot_reconcile_is_retried_before_convergence(mgr, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "cluster")
    seen = []

    async def reconcile(*, trigger="manual"):
        seen.append(trigger)
        if len(seen) == 1:
            raise RuntimeError("db hiccup")
        return {}

    monkeypatch.setattr(mgr, "reconcile_cluster_state", reconcile)

    async def run():
        await mgr._activate_saved_plugins()          # must not raise
        mgr._boot_watch_task.cancel()
        assert mgr.boot_converged is False
        await mgr._watch_boot_convergence(poll_s=0.01, retry_s=0.01)

    asyncio.run(run())
    assert seen == ["boot", "boot-retry"] and mgr.boot_converged is True


def test_nested_and_later_passes_do_not_restart_the_watch(mgr, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "single")

    async def run():
        await mgr._activate_saved_plugins()
        first = mgr._boot_watch_task
        await _settle()
        assert mgr.boot_converged is True
        await mgr._activate_saved_plugins()          # pass after a later install
        assert mgr._boot_watch_task is first and mgr.boot_converged is True

    asyncio.run(run())


def test_a_pass_that_dies_hands_the_boot_role_to_the_next_one(mgr, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "single")
    calls = []

    def flaky(loaded):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database went away")
        return {}

    monkeypatch.setattr(mgr, "_restore_row_states", flaky)

    async def run():
        with pytest.raises(RuntimeError):
            await mgr._activate_saved_plugins()
        assert mgr.boot_converged is False and mgr._boot_watch_task is None
        await mgr._activate_saved_plugins()          # db:connected fires again
        await _settle()
        assert mgr.boot_converged is True

    asyncio.run(run())
