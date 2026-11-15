"""BG-14: the bus de-duplicates subscribers by equality, not by ``module:qualname``.

Bound methods of two instances, closures produced by a factory and lambdas share
a qualified name; only the first of them used to be registered — silently.
"""
import asyncio
import types

from core.bus import GlobalEventBus

TOPIC = "test:dedup"


class _Service:
    def __init__(self, name, seen):
        self.name, self.seen = name, seen

    def on_event(self, payload):
        self.seen.append(self.name)


def test_bound_methods_of_two_instances_are_both_delivered():
    bus, seen = GlobalEventBus(), []
    a, b = _Service("a", seen), _Service("b", seen)
    bus.subscribe(TOPIC)(a.on_event)
    bus.subscribe(TOPIC)(b.on_event)

    bus.emit(TOPIC, {})
    assert seen == ["a", "b"]


def test_the_same_callback_subscribed_twice_fires_once():
    bus, seen = GlobalEventBus(), []
    svc = _Service("a", seen)

    def handler(payload):
        seen.append("fn")

    for _ in range(2):
        bus.subscribe(TOPIC)(svc.on_event)      # a new bound-method object each time
        bus.subscribe(TOPIC)(handler)

    bus.emit(TOPIC, {})
    assert seen == ["a", "fn"]


def test_factory_closures_and_lambdas_are_all_delivered():
    bus, seen = GlobalEventBus(), []

    def make_handler(tag):
        def handler(payload):
            seen.append(tag)
        return handler

    bus.subscribe(TOPIC)(make_handler("x"))
    bus.subscribe(TOPIC)(make_handler("y"))
    bus.subscribe(TOPIC)(lambda payload: seen.append("l1"))
    bus.subscribe(TOPIC)(lambda payload: seen.append("l2"))

    bus.emit(TOPIC, {})
    assert seen == ["x", "y", "l1", "l2"]


def test_unsubscribe_matches_a_bound_method_by_equality():
    bus, seen = GlobalEventBus(), []
    a, b = _Service("a", seen), _Service("b", seen)
    bus.subscribe(TOPIC)(a.on_event)
    bus.subscribe(TOPIC)(b.on_event)

    assert bus.unsubscribe(TOPIC, a.on_event) is True       # not the object that was subscribed
    assert bus.unsubscribe(TOPIC, a.on_event) is False
    bus.emit(TOPIC, {})
    assert seen == ["b"]


def test_handler_may_unsubscribe_itself_during_dispatch():
    bus, seen = GlobalEventBus(), []

    def once(payload):
        seen.append("once")
        bus.unsubscribe(TOPIC, once)

    bus.subscribe(TOPIC)(once)
    bus.subscribe(TOPIC)(lambda payload: seen.append("other"))
    bus.emit(TOPIC, {})
    bus.emit(TOPIC, {})
    assert seen == ["once", "other", "other"]


def test_unsubscribe_package_removes_only_that_package():
    bus, seen = GlobalEventBus(), []

    def _fn(module, tag):
        def handler(payload):
            seen.append(tag)
        handler.__module__ = module
        return handler

    bus.subscribe(TOPIC)(_fn("plugins.mon.entrypoint", "mon"))
    bus.subscribe(TOPIC)(_fn("plugins.mon.app.logic", "mon.logic"))
    bus.subscribe(TOPIC)(_fn("plugins.mon_agent.entrypoint", "agent"))

    assert bus.unsubscribe_package("plugins.mon") == 2
    bus.emit(TOPIC, {})
    assert seen == ["agent"]


# --- a plugin reload / retried setup must still not double-subscribe -------------

def _context(topics):
    import core.services  # noqa: F401  (import order: services before api)
    from core.components.plugins.logic.context import ModuleContext
    from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

    manifest = ModuleManifest(id="lyndrix.plugin.test_bus_dedup", name="Dedup", version="0.0.1", type="PLUGIN",
                              permissions=ModulePermissions(emit=[], subscribe=list(topics)))
    return ModuleContext(manifest)


def test_reenabled_plugin_does_not_double_subscribe(monkeypatch):
    """setup() creates fresh closures on every run; dispose() (disable) must have
    removed the previous ones."""
    from core.bus import bus as global_bus
    from core.components.plugins.logic import manager as manager_mod
    from core.components.plugins.logic.manager import ModuleManager

    seen = []
    topic = "test:dedup_reenable"

    def setup(ctx):
        @ctx.subscribe(topic)
        def _on_event(payload):
            seen.append("hit")

    ctx = _context([topic])
    mgr = ModuleManager()
    monkeypatch.setattr(manager_mod.db_instance, "SessionLocal", None, raising=False)
    monkeypatch.setattr(mgr, "_ensure_plugin_state_schema", lambda: None)
    monkeypatch.setattr(mgr, "_teardown_ui", lambda mid: None)
    monkeypatch.setattr(mgr, "_mount_plugin_statics", lambda mid: None)
    module = types.SimpleNamespace(setup=setup, __name__="plugins.test_bus_dedup.entrypoint",
                                   __file__="/nonexistent/plugins/test_bus_dedup/entrypoint.py")
    mgr.registry[ctx.manifest.id] = {"manifest": ctx.manifest, "module": module, "context": ctx,
                                     "status": "initializing", "error_message": None}
    try:
        mgr.toggle_module(ctx.manifest.id, True, persist=False)
        mgr.toggle_module(ctx.manifest.id, False, persist=False)
        mgr.toggle_module(ctx.manifest.id, True, persist=False)
        global_bus.emit(topic, {})
        assert seen == ["hit"]
    finally:
        ctx.dispose()


def test_retried_setup_after_a_failure_does_not_double_subscribe(monkeypatch):
    """A failed setup() is disposed, so the retry subscribes each handler once."""
    from core.bus import bus as global_bus
    from core.components.plugins.logic.manager import ModuleManager

    seen, attempts = [], []
    topic = "test:dedup_retry"

    def setup(ctx):
        @ctx.subscribe(topic)
        def _on_event(payload):
            seen.append("hit")

        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails after subscribing")

    ctx = _context([topic])
    mgr = ModuleManager()
    monkeypatch.setattr(mgr, "_mount_plugin_statics", lambda mid: None)
    module = types.SimpleNamespace(setup=setup, __name__="plugins.test_bus_dedup.entrypoint",
                                   __file__="/nonexistent/plugins/test_bus_dedup/entrypoint.py")
    entry = {"manifest": ctx.manifest, "module": module, "context": ctx,
             "status": "initializing", "error_message": None}
    mgr.registry[ctx.manifest.id] = entry
    try:
        mgr._execute_setup(ctx.manifest.id)
        assert entry["status"] == "failed"
        global_bus.emit(topic, {})
        assert seen == []                        # the failed attempt left no handler behind

        mgr._execute_setup(ctx.manifest.id)      # next activation pass retries
        assert entry["status"] == "active"
        global_bus.emit(topic, {})
        assert seen == ["hit"]
    finally:
        ctx.dispose()


def test_only_the_process_wide_manager_listens_on_the_bus():
    """Managers built by tests must not handle the process's lifecycle events."""
    from core.bus import bus as global_bus
    from core.components.plugins.logic.manager import ModuleManager, module_manager

    ModuleManager()
    ModuleManager()
    handlers = global_bus.subscribers.get("plugin:files_changed", [])
    owners = [getattr(h, "__self__", None) for h in handlers
              if getattr(h, "__name__", "") == "_handle_plugin_change"]
    assert owners == [module_manager]
    asyncio.run(asyncio.sleep(0))
