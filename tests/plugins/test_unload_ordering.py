"""CL-11: teardown → dispose → lease release → purge, with the teardown awaited.

(a) a sync teardown that schedules its stop work with ``ctx.create_task`` (IaC
    orchestrator) — ``dispose()`` used to cancel that task before its first step;
(b) an async teardown (monitoring, docker-manager) ran un-awaited while
    ``dispose()`` released the lease — a peer took the work over while the old
    incarnation was still draining.
"""
import asyncio
import types

import pytest

import core.services  # noqa: F401  (import order: services before api)
from core.components.cluster.logic.cluster_service import cluster_service
from core.components.plugins.logic import manager as manager_mod
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

MID = "lyndrix.plugin.cl11_probe"
LEASE = "cl11:worker"


class _RecordingCoordinator:
    """Wraps the real (local) coordinator and records the moment of a release."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def __getattr__(self, item):
        return getattr(self._inner, item)

    async def release(self, name):
        self._log.append(f"release:{name}")
        return await self._inner.release(name)


@pytest.fixture()
def env(monkeypatch):
    log: list[str] = []
    mgr = ModuleManager()
    monkeypatch.setattr(mgr, "_teardown_ui", lambda mid: None)
    monkeypatch.setattr(mgr, "_unmount_plugin_statics", lambda mid: None)
    monkeypatch.setattr(mgr, "_mount_plugin_statics", lambda mid: None)
    monkeypatch.setattr(manager_mod.db_instance, "SessionLocal", None, raising=False)
    monkeypatch.setattr(cluster_service.leases, "_coordinator",
                        _RecordingCoordinator(cluster_service.leases._coordinator, log))

    def register(teardown, setup=lambda ctx: None) -> ModuleContext:
        manifest = ModuleManifest(id=MID, name="CL-11", version="1.0.0", type="PLUGIN",
                                  permissions=ModulePermissions(emit=[], subscribe=[]))
        module = types.SimpleNamespace(teardown=teardown, setup=setup,
                                       __name__="plugins.cl11_probe.entrypoint",
                                       __file__="/nonexistent/plugins/cl11_probe/entrypoint.py")
        ctx = ModuleContext(manifest)
        mgr.registry[MID] = {"manifest": manifest, "module": module, "context": ctx,
                             "status": "active", "error_message": None}
        return ctx

    try:
        yield mgr, register, log
    finally:
        entry = mgr.registry.pop(MID, None)
        if entry:
            entry["context"].dispose()


def test_stop_task_scheduled_in_a_sync_teardown_runs(env):
    mgr, register, log = env

    async def run():
        async def worker():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                log.append("worker cancelled")
                raise

        async def stop_runner():
            await asyncio.sleep(0.05)
            log.append("stop_runner ran")

        ctx = register(lambda c: c.create_task(stop_runner(), name="probe:stop"))
        ctx.create_task(worker(), name="probe:worker")     # existed before teardown
        await asyncio.sleep(0)

        assert await mgr.unload_module_async(MID) is True
        await asyncio.sleep(0.01)
        assert MID not in mgr.registry

    asyncio.run(run())
    # The stop work finished before dispose cancelled the old worker.
    assert log == ["stop_runner ran", "worker cancelled"]


def test_lease_is_released_after_an_async_teardown_finished(env):
    mgr, register, log = env
    lost = []

    async def run():
        async def teardown(ctx):
            log.append("teardown started")
            await asyncio.sleep(0.1)
            log.append("teardown finished")

        ctx = register(teardown)
        ctx.on_lease(LEASE, acquired=lambda: log.append("acquired"), lost=lambda: lost.append("lost"))
        await cluster_service.leases.tick()
        assert ctx.holds_lease(LEASE)

        assert await mgr.unload_module_async(MID) is True
        await asyncio.sleep(0.05)
        assert not cluster_service.leases.holds(LEASE)

    asyncio.run(run())
    assert log == ["acquired", "teardown started", "teardown finished", f"release:{LEASE}"]
    assert lost == []                                      # still no `lost` on dispose


def test_disable_waits_for_the_teardown_too_and_reenable_starts_clean(env):
    mgr, register, log = env

    async def run():
        async def teardown(ctx):
            await asyncio.sleep(0.05)
            log.append("teardown finished")

        def setup(ctx):
            log.append("setup")

        ctx = register(teardown, setup)
        ctx.on_lease(LEASE, acquired=lambda: None)
        await cluster_service.leases.tick()

        # disable and enable issued back to back: the enable waits for the stop
        off = asyncio.ensure_future(mgr.set_module_active(MID, False, persist=False))
        await asyncio.sleep(0)
        on = asyncio.ensure_future(mgr.set_module_active(MID, True, persist=False))
        assert await off is True and await on is True
        assert mgr.registry[MID]["status"] == "active" and "stopping" not in mgr.registry[MID]

    asyncio.run(run())
    assert log == ["teardown finished", f"release:{LEASE}", "setup"]


def test_teardown_is_bounded_by_the_timeout(env, monkeypatch):
    mgr, register, log = env
    monkeypatch.setenv("LYNDRIX_PLUGIN_TEARDOWN_TIMEOUT", "0.05")
    assert mgr._teardown_timeout() == 0.05

    async def run():
        async def teardown(ctx):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                log.append("teardown cancelled")
                raise

        register(teardown)
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await mgr.unload_module_async(MID) is True
        await asyncio.sleep(0.01)
        assert loop.time() - started < 2
        assert MID not in mgr.registry

    asyncio.run(run())
    assert log == ["teardown cancelled"]


def test_teardown_timeout_default_and_invalid_values(monkeypatch):
    monkeypatch.delenv("LYNDRIX_PLUGIN_TEARDOWN_TIMEOUT", raising=False)
    assert ModuleManager._teardown_timeout() == 15.0
    monkeypatch.setenv("LYNDRIX_PLUGIN_TEARDOWN_TIMEOUT", "not-a-number")
    assert ModuleManager._teardown_timeout() == 15.0


def test_no_setup_while_a_module_is_stopping(env):
    """An activation pass that runs while teardown is awaited must not set the
    module up again."""
    mgr, register, log = env

    async def run():
        async def teardown(ctx):
            await asyncio.sleep(0.05)

        register(teardown, setup=lambda ctx: log.append("setup"))
        stop = asyncio.ensure_future(mgr.set_module_active(MID, False, persist=False))
        await asyncio.sleep(0.01)
        assert mgr.registry[MID].get("stopping") is True
        mgr._execute_setup(MID)                             # e.g. _activate_saved_plugins
        await stop

    asyncio.run(run())
    assert log == []


def test_sync_unload_no_longer_cancels_the_teardown_stop_task(env):
    """Legacy sync path: it cannot wait, but it must not cancel the stop work."""
    mgr, register, log = env

    async def run():
        async def stop_runner():
            log.append("stop_runner ran")

        register(lambda c: c.create_task(stop_runner(), name="probe:stop"))
        assert mgr.unload_module(MID) is True
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert log == ["stop_runner ran"]
