"""BG-12 / CL-15: plugin lifecycle DB work stays off the event loop.

- ``_activate_saved_plugins`` does its schema bootstrap, row inserts and the
  retried row sync in one worker thread;
- ``set_module_active`` (async ``toggle_module``) writes ``plugin_states`` in a
  worker thread;
- the lifecycle notification handlers look the tracking operation up in a
  worker thread instead of querying the DB inside a sync bus subscriber.
"""
import asyncio
import threading
import time
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import db_service
from core.components.plugins import entrypoint as plugins_entrypoint
from core.components.plugins.logic import operations as ops_mod
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import (
    ModuleManifest, ModulePermissions, PluginOperation, PluginOperationInstance, PluginState,
)

MID = "lyndrix.plugin.offloop"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for model in (PluginState, PluginOperation, PluginOperationInstance):
        model.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    return Session


def _manager(monkeypatch, setup=lambda ctx: None) -> ModuleManager:
    mgr = ModuleManager()

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mgr, "_ensure_plugin_state_schema", lambda: None)
    monkeypatch.setattr(mgr, "_converge_after_restore", _noop)
    monkeypatch.setattr(mgr, "_mount_plugin_statics", lambda mid: None)
    monkeypatch.setattr(mgr, "_unmount_plugin_statics", lambda mid: None)
    monkeypatch.setattr(mgr, "_teardown_ui", lambda mid: None)
    manifest = ModuleManifest(id=MID, name="Off-loop", version="1.2.3", type="PLUGIN", auto_enable_on_install=True,
                              permissions=ModulePermissions(emit=[], subscribe=[]))
    module = types.SimpleNamespace(setup=setup, __name__="plugins.offloop.entrypoint",
                                   __file__="/nonexistent/plugins/offloop/entrypoint.py")
    mgr.registry[MID] = {"manifest": manifest, "module": module, "context": ModuleContext(manifest),
                         "status": "initializing", "error_message": None, "persisted": False}
    return mgr


def test_activation_does_its_db_work_in_a_worker_thread(db, monkeypatch):
    setup_threads = []
    mgr = _manager(monkeypatch, setup=lambda ctx: setup_threads.append(threading.get_ident()))
    db_threads = []
    real = mgr._restore_row_states

    def spy(loaded):
        db_threads.append(threading.get_ident())
        return real(loaded)

    monkeypatch.setattr(mgr, "_restore_row_states", spy)
    loop_thread = threading.get_ident()

    asyncio.run(mgr._activate_saved_plugins())

    assert db_threads and db_threads[0] != loop_thread          # DB work: worker thread
    assert setup_threads == [loop_thread]                        # setup(): event loop
    with db() as s:
        row = s.get(PluginState, MID)
        assert row.is_active is True and row.installed_version == "1.2.3"
    assert mgr.registry[MID]["status"] == "active" and mgr.registry[MID]["persisted"] is True


def test_set_module_active_persists_in_a_worker_thread(db, monkeypatch):
    mgr = _manager(monkeypatch)
    with db() as s:
        s.add(PluginState(module_id=MID, is_active=False, installed_version="1.2.3"))
        s.commit()
    threads = []
    real = mgr._persist_toggle
    monkeypatch.setattr(mgr, "_persist_toggle",
                        lambda mid, active: (threads.append(threading.get_ident()), real(mid, active))[1])
    loop_thread = threading.get_ident()

    async def run():
        assert await mgr.set_module_active(MID, True) is True
        assert mgr.registry[MID]["status"] == "active"
        assert await mgr.set_module_active(MID, False) is True
        assert await mgr.set_module_active("lyndrix.plugin.unknown", True) is False

    asyncio.run(run())

    assert len(threads) == 2 and all(t != loop_thread for t in threads)
    with db() as s:
        assert s.get(PluginState, MID).is_active is False
    assert mgr.registry[MID]["status"] == "disabled"


def test_failed_toggle_write_leaves_the_plugin_untouched(db, monkeypatch):
    mgr = _manager(monkeypatch)
    mgr.registry[MID]["status"] = "active"
    monkeypatch.setattr(mgr, "_persist_toggle", lambda mid, active: False)
    assert asyncio.run(mgr.set_module_active(MID, False)) is False
    assert mgr.registry[MID]["status"] == "active"


# --- lifecycle notification handlers (CL-15) -------------------------------------

class _FakeCtx:
    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.notified: list[str] = []

    def subscribe(self, topic):
        def decorator(fn):
            self.handlers.setdefault(topic, []).append(fn)
            return fn
        return decorator

    def notify(self, endpoint, **kwargs):
        self.notified.append(endpoint)


def _handlers():
    ctx = _FakeCtx()
    plugins_entrypoint._register_lifecycle_notifications(ctx)
    return ctx


def test_notification_handlers_do_not_query_the_db_on_the_loop(monkeypatch):
    ctx = _handlers()
    lookups = []

    def slow_tracks(*, repo_name, grace_s=60):
        lookups.append(threading.get_ident())
        time.sleep(0.3)                         # slow / black-holed database
        return False

    monkeypatch.setattr(ops_mod.operation_tracker, "tracks", slow_tracks)
    loop_thread = threading.get_ident()

    async def run():
        handler = ctx.handlers["plugin:installed"][0]
        assert asyncio.iscoroutinefunction(handler)      # the bus runs it as a task
        task = asyncio.ensure_future(handler({"repo": "lyndrix-plugin-x"}))
        worst = 0.0
        while not task.done():                           # loop latency probe
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - t0 - 0.01)
        await task
        return worst

    worst = asyncio.run(run())
    assert lookups and lookups[0] != loop_thread
    assert worst < 0.1, f"event loop stalled {worst:.3f}s"
    assert ctx.notified == ["plugin_installed"]


def test_event_of_a_tracked_or_just_closed_operation_is_not_notified_twice(db, monkeypatch):
    tracker = ops_mod.operation_tracker
    monkeypatch.setattr(tracker, "live_instances", lambda: ["a"])
    monkeypatch.setattr(tracker, "instance_id", lambda: "a")
    ctx = _handlers()
    rolled_back = ctx.handlers["plugin:rolled_back"][0]

    tracker.start(plugin_id=None, repo_name="lyndrix_plugin_x", action="upgrade", version="1.4.0",
                  requested_by="admin")
    asyncio.run(rolled_back({"repo": "lyndrix_plugin_x", "version": "1.3.0"}))
    assert ctx.notified == []                            # the running operation will notify

    # The operation closes before the (asynchronous) lookup of a late event runs.
    tracker.report(plugin_id=None, repo_name="lyndrix_plugin_x", status="reverted")
    assert tracker.has_running(repo_name="lyndrix_plugin_x") is False
    asyncio.run(rolled_back({"repo": "lyndrix_plugin_x", "version": "1.3.0"}))
    assert ctx.notified == []

    # … but an old, closed operation does not swallow a later untracked event.
    assert tracker.tracks(repo_name="lyndrix_plugin_x", grace_s=0) is False
    asyncio.run(rolled_back({"repo": "another_repo", "version": "1.0.0"}))
    assert ctx.notified == ["plugin_rolled_back"]


def test_peer_events_are_still_ignored_by_async_handlers(monkeypatch):
    ctx = _handlers()
    monkeypatch.setattr(ops_mod.operation_tracker, "tracks", lambda **k: False)
    failed = ctx.handlers["plugin:install_failed"][0]
    asyncio.run(failed({"repo": "x", "error": "boom", "_origin": "peer-1"}))
    assert ctx.notified == []
    asyncio.run(failed({"repo": "x", "error": "boom"}))
    assert ctx.notified == ["plugin_install_failed"]
