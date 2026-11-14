"""BG-06: the restore loop of ``_activate_saved_plugins`` and async ``setup()``.

- only ``blocked`` plugins are retried — a ``degraded`` one (critical manifest
  issue) used to be reset to "initializing" and set up anyway;
- an async ``setup()`` runs exactly once, also across repeated activation passes;
- it is owned by the context (``dispose()`` cancels it) and a task that finishes
  after a disable/reload cannot flip the status of the entry.
"""
import asyncio
import types

import pytest

import core.services  # noqa: F401  (import order: services before api)
from core.components.plugins.logic import manager as manager_mod
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions


def _entry(mid: str, status: str, setup, *, dependencies=None) -> dict:
    manifest = ModuleManifest(
        id=mid, name=mid, version="1.0.0", type="PLUGIN",
        permissions=ModulePermissions(emit=[], subscribe=[]),
        dependencies=dependencies or [],
    )
    module = types.SimpleNamespace(setup=setup, __file__=f"/nonexistent/{mid}/entrypoint.py",
                                   __name__=f"plugins.{mid.replace('.', '_')}.entrypoint")
    return {"manifest": manifest, "module": module, "context": ModuleContext(manifest),
            "status": status, "error_message": None}


@pytest.fixture()
def mgr(monkeypatch):
    """A manager whose DB parts are stubbed; ``row_states`` = plugin_states.is_active."""
    m = ModuleManager()
    m.row_states = {}

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(manager_mod.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(m, "_ensure_plugin_state_schema", lambda: None)
    monkeypatch.setattr(m, "_restore_row_states", lambda loaded: dict(m.row_states))
    monkeypatch.setattr(m, "_converge_after_restore", _noop)
    monkeypatch.setattr(m, "_mount_plugin_statics", lambda mid: None)
    monkeypatch.setattr(m, "_unmount_plugin_statics", lambda mid: None)
    monkeypatch.setattr(m, "_teardown_ui", lambda mid: None)
    return m


def test_degraded_plugin_is_never_set_up(mgr):
    calls = []
    mgr.registry["p.degraded"] = _entry("p.degraded", "degraded", lambda ctx: calls.append("setup"))
    mgr.row_states = {"p.degraded": True}

    asyncio.run(mgr._activate_saved_plugins())

    assert calls == []
    assert mgr.registry["p.degraded"]["status"] == "degraded"


def test_failed_sync_setup_is_not_retried_in_the_same_pass(mgr):
    calls = []

    def setup(ctx):
        calls.append("setup")
        raise RuntimeError("boom")

    mgr.registry["p.failed"] = _entry("p.failed", "initializing", setup)
    mgr.row_states = {"p.failed": True}

    asyncio.run(mgr._activate_saved_plugins())

    assert calls == ["setup"] and mgr.registry["p.failed"]["status"] == "failed"


def test_blocked_plugin_starts_once_its_dependency_is_active(mgr):
    order = []
    dep = {"id": "p.base"}
    # Registry order puts the dependent first: it is parked "blocked" in the
    # first sweep and picked up by the retry.
    mgr.registry["p.child"] = _entry("p.child", "initializing", lambda ctx: order.append("child"),
                                     dependencies=[dep])
    mgr.registry["p.base"] = _entry("p.base", "initializing", lambda ctx: order.append("base"))
    mgr.row_states = {"p.child": True, "p.base": True}

    asyncio.run(mgr._activate_saved_plugins())

    assert order == ["base", "child"]
    assert mgr.registry["p.child"]["status"] == "active"


def test_async_setup_runs_once_across_activation_passes(mgr):
    calls = []
    release = asyncio.Event

    async def run():
        gate = release()

        async def setup(ctx):
            calls.append("setup")
            await gate.wait()

        mgr.registry["p.async"] = _entry("p.async", "initializing", setup)
        mgr.row_states = {"p.async": True}

        await mgr._activate_saved_plugins()
        await asyncio.sleep(0)
        await mgr._activate_saved_plugins()      # e.g. the pass after an install event
        await asyncio.sleep(0)
        assert calls == ["setup"] and mgr.registry["p.async"]["status"] == "initializing"

        gate.set()
        await asyncio.sleep(0.01)
        assert calls == ["setup"] and mgr.registry["p.async"]["status"] == "active"

    asyncio.run(run())


def test_async_setup_completion_unblocks_dependents(mgr):
    order = []

    async def run():
        gate = asyncio.Event()

        async def base_setup(ctx):
            await gate.wait()
            order.append("base")

        mgr.registry["p.base"] = _entry("p.base", "initializing", base_setup)
        mgr.registry["p.child"] = _entry("p.child", "initializing", lambda ctx: order.append("child"),
                                         dependencies=[{"id": "p.base"}])
        mgr.row_states = {"p.base": True, "p.child": True}

        await mgr._activate_saved_plugins()
        assert mgr.registry["p.child"]["status"] == "blocked"
        gate.set()
        await asyncio.sleep(0.01)
        assert order == ["base", "child"] and mgr.registry["p.child"]["status"] == "active"

    asyncio.run(run())


def test_disable_cancels_a_running_async_setup(mgr):
    seen = []

    async def run():
        async def setup(ctx):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                seen.append("cancelled")
                raise

        mgr.registry["p.slow"] = _entry("p.slow", "initializing", setup)
        mgr.row_states = {"p.slow": True}
        await mgr._activate_saved_plugins()
        await asyncio.sleep(0)

        assert mgr.toggle_module("p.slow", False, persist=False) is True
        await asyncio.sleep(0.01)
        assert seen == ["cancelled"] and mgr.registry["p.slow"]["status"] == "disabled"

    asyncio.run(run())


def test_stale_setup_task_cannot_flip_a_disabled_or_reloaded_entry(mgr):
    """A setup() that swallows the cancellation and finishes late must not mark
    the disabled entry — or the next incarnation — active/failed."""

    async def run():
        async def stubborn_setup(ctx):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                pass                          # ignores the cancel and "finishes"

        async def stubborn_failing_setup(ctx):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise RuntimeError("late failure")

        # disabled while setting up
        mgr.registry["p.a"] = _entry("p.a", "initializing", stubborn_setup)
        mgr.row_states = {"p.a": True}
        await mgr._activate_saved_plugins()
        await asyncio.sleep(0)
        mgr.toggle_module("p.a", False, persist=False)
        await asyncio.sleep(0.01)
        assert mgr.registry["p.a"]["status"] == "disabled"

        # replaced by a reload while setting up: the new entry keeps its own status
        old = _entry("p.b", "initializing", stubborn_failing_setup)
        mgr.registry["p.b"] = old
        mgr.row_states = {"p.b": True}
        await mgr._activate_saved_plugins()
        await asyncio.sleep(0)
        old["context"].dispose()                                   # unload_module
        mgr.registry["p.b"] = _entry("p.b", "initializing", lambda ctx: None)
        await asyncio.sleep(0.01)
        assert mgr.registry["p.b"]["status"] == "initializing"
        assert mgr.registry["p.b"]["error_message"] is None

    asyncio.run(run())


def test_running_plugin_is_stopped_when_its_row_says_inactive(mgr):
    """Step 2 used to flip the status to "disabled" only: tasks, subscriptions,
    leases and routes of the plugin stayed alive behind the label."""
    log = []

    async def run():
        async def teardown(ctx):
            await asyncio.sleep(0.01)
            log.append("teardown")

        async def worker():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                log.append("worker cancelled")
                raise

        entry = _entry("p.live", "active", lambda ctx: None)
        entry["module"].teardown = teardown
        entry["context"].create_task(worker(), name="p.live:worker")
        mgr.registry["p.live"] = entry
        # never set up (fresh load) and disabled in the row: no teardown, just the label
        fresh = _entry("p.fresh", "initializing", lambda ctx: log.append("fresh setup"))
        fresh["module"].teardown = lambda ctx: log.append("fresh teardown")
        mgr.registry["p.fresh"] = fresh
        mgr.row_states = {"p.live": False, "p.fresh": False}
        await asyncio.sleep(0)

        await mgr._activate_saved_plugins()
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert log == ["teardown", "worker cancelled"]
    assert mgr.registry["p.live"]["status"] == "disabled"
    assert mgr.registry["p.fresh"]["status"] == "disabled"


def test_enabling_a_plugin_activates_the_plugins_blocked_on_it(mgr):
    """Follow-up to BG-06: dependents used to stay "blocked" until some later
    activation pass (the next install event or DB reconnect)."""
    order = []
    mgr.registry["p.base"] = _entry("p.base", "disabled", lambda ctx: order.append("base"))
    mgr.registry["p.child"] = _entry("p.child", "blocked", lambda ctx: order.append("child"),
                                     dependencies=[{"id": "p.base"}])
    mgr.registry["p.grandchild"] = _entry("p.grandchild", "blocked", lambda ctx: order.append("grandchild"),
                                          dependencies=[{"id": "p.child"}])
    mgr.registry["p.other"] = _entry("p.other", "disabled", lambda ctx: order.append("other"))

    async def run():
        assert await mgr.set_module_active("p.base", True, persist=False) is True

    asyncio.run(run())
    assert order == ["base", "child", "grandchild"]            # disabled plugins stay off
    assert [mgr.registry[m]["status"] for m in ("p.child", "p.grandchild", "p.other")] == \
        ["active", "active", "disabled"]

    # the sync toggle does the same
    for mid in ("p.child", "p.grandchild"):
        mgr.registry[mid]["status"] = "blocked"
    mgr.registry["p.base"]["status"] = "disabled"
    order.clear()
    assert mgr.toggle_module("p.base", True, persist=False) is True
    assert order == ["base", "child", "grandchild"]
