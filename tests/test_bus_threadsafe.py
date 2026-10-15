"""GlobalEventBus.emit() from a worker thread hops onto the application loop
(core 1.7): sync and async subscribers both run on the loop thread."""
import asyncio
import threading

from core.bus import bus

TOPIC = "test:threadsafe_emit"


def test_emit_from_worker_thread_dispatches_on_the_loop_thread():
    seen: list[tuple[str, int]] = []

    async def async_handler(payload):
        seen.append(("async", threading.get_ident()))

    def sync_handler(payload):
        seen.append(("sync", threading.get_ident()))

    async_handler.__qualname__ = f"test_bus_async<{id(seen)}>"
    sync_handler.__qualname__ = f"test_bus_sync<{id(seen)}>"
    bus.subscribe(TOPIC)(async_handler)
    bus.subscribe(TOPIC)(sync_handler)
    loop_thread = threading.get_ident()

    async def main():
        bus.bind_loop(asyncio.get_running_loop())
        await asyncio.to_thread(bus.emit, TOPIC, {"n": 1})   # worker thread
        bus.emit(TOPIC, {"n": 2})                             # loop thread
        await asyncio.sleep(0.05)

    try:
        asyncio.run(main())
    finally:
        bus.unsubscribe(TOPIC, async_handler)
        bus.unsubscribe(TOPIC, sync_handler)
        bus.bind_loop(None)

    assert len(seen) == 4
    assert all(thread == loop_thread for _, thread in seen)
    assert sorted(kind for kind, _ in seen) == ["async", "async", "sync", "sync"]


def test_emit_without_any_loop_still_dispatches_sync_subscribers_inline():
    seen = []

    def handler(payload):
        seen.append(payload["x"])

    handler.__qualname__ = f"test_bus_inline<{id(seen)}>"
    bus.subscribe(TOPIC)(handler)
    try:
        bus.bind_loop(None)
        bus.emit(TOPIC, {"x": 42})
    finally:
        bus.unsubscribe(TOPIC, handler)
    assert seen == [42]
