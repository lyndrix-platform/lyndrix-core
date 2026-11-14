"""CL-10: no event is skipped because its row was committed late, and the
bridge publishes in order through an outbox that survives a database outage."""
import asyncio
import random
from datetime import datetime

import pytest
from sqlalchemy import insert

from cluster_harness import make_engine
from core.bus import bus
from core.components.cluster.logic import bridge as bridge_mod
from core.components.cluster.logic.bridge import ClusterBridge
from core.components.cluster.logic.drivers import db as db_mod
from core.components.cluster.logic.drivers.db import DbCoordinator
from core.components.cluster.logic.models import ClusterEvent


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(tmp_path)
    yield eng
    eng.dispose()


def _reader(engine, iid="pod-3"):
    coord = DbCoordinator(instance_id=iid, ttl=30, heartbeat_s=10, poll_ms=100, engine_getter=lambda: engine)
    coord.ensure_schema()
    got: list = []
    coord.subscribe(lambda topic, payload, origin: got.append((topic, origin)))
    return coord, got


def _commit(engine, event_id, topic, origin):
    with engine.begin() as conn:
        conn.execute(insert(ClusterEvent).values(id=event_id, topic=topic, payload={}, origin=origin,
                                                 created_at=datetime.utcnow()))


# --- reader: late-committed ids -----------------------------------------------------

def test_an_event_committed_after_a_higher_id_is_still_delivered(engine):
    """p8: pod-1 INSERTs id 11 (slow COMMIT), pod-2 INSERTs id 12 and commits
    first, the reader polls in between. With "id > cursor" id 11 was lost."""
    reader, got = _reader(engine)

    async def run():
        _commit(engine, 10, "ui:needs_refresh", "pod-1")     # history from before the reader joined
        await reader.start()
        _commit(engine, 12, "settings:changed", "pod-2")
        await asyncio.sleep(0.35)                            # reader polls: sees 12
        assert got == [("settings:changed", "pod-2")]
        _commit(engine, 11, "plugin:desired_changed", "pod-1")   # the late commit lands
        await asyncio.sleep(0.5)
        await reader.stop()

    asyncio.run(run())
    assert got == [("settings:changed", "pod-2"), ("plugin:desired_changed", "pod-1")]   # each exactly once
    assert reader.events_late == 1 and reader._last_event_id == 12


def test_history_is_not_replayed_and_the_seen_set_stays_bounded(engine, monkeypatch):
    monkeypatch.setattr(db_mod, "_EVENT_REREAD_WINDOW", 20)
    reader, got = _reader(engine)

    async def run():
        for i in range(1, 31):
            _commit(engine, i, "old:event", "pod-1")         # before the reader joined
        await reader.start()
        for i in range(31, 131):
            _commit(engine, i, "new:event", "pod-1")
            if i % 25 == 0:
                await asyncio.sleep(0.25)
        _commit(engine, 200, "own:event", "pod-3")           # the reader's own event
        await asyncio.sleep(0.4)
        await reader.stop()

    asyncio.run(run())
    assert [t for t, _ in got] == ["new:event"] * 100        # no history, no duplicates, not its own
    assert len(reader._seen_event_ids) <= 20 and reader.events_late == 0


# --- publisher: order, outage, bounds -------------------------------------------------

class FlakyCoordinator:
    driver, instance_id = "fake", "me"

    def __init__(self):
        self.published: list = []
        self.fail_next = 0
        self.jitter = False
        self.callbacks: list = []

    async def publish(self, topic, payload):
        if self.jitter:
            await asyncio.sleep(random.uniform(0, 0.01))     # INSERT latency varies from call to call
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("(2013, 'Lost connection to MySQL server during query')")
        self.published.append((topic, payload.get("n")))

    def subscribe(self, callback):
        self.callbacks.append(callback)


def _attached(coord):
    bridge = ClusterBridge(coord, "me")
    bridge.retry_delays = (0.01,)
    return bridge


def test_events_of_one_instance_are_published_in_the_order_they_were_emitted():
    coord = FlakyCoordinator()
    coord.jitter = True
    bridge = _attached(coord)

    async def run():
        bridge.attach()
        try:
            for n in range(40):
                bus.emit("plugin:runtime_status", {"n": n})  # e.g. starting → … → ready
            await bridge.flush(timeout=3)
        finally:
            bridge.detach()

    asyncio.run(run())
    assert [n for _, n in coord.published] == list(range(40))


def test_events_emitted_during_a_database_outage_are_delivered_afterwards():
    coord = FlakyCoordinator()
    coord.fail_next = 6                                      # the database is away for a moment
    bridge = _attached(coord)

    async def run():
        bridge.attach()
        try:
            bus.emit("settings:changed", {"n": 1})
            bus.emit("plugin:desired_changed", {"n": 2})
            bus.emit("monitoring:monitors_changed", {"n": 3}, cluster=True)
            assert await bridge.flush(timeout=3) is True
        finally:
            bridge.detach()

    asyncio.run(run())
    assert coord.published == [("settings:changed", 1), ("plugin:desired_changed", 2),
                               ("monitoring:monitors_changed", 3)]
    status = bridge.status()
    assert status["failures"] == 6 and status["published"] == 3 and status["outbox"] == 0
    assert status["dropped"] == status["expired"] == status["oversized"] == 0


def test_outbox_is_bounded_oldest_first_and_stale_or_oversized_events_are_not_sent(monkeypatch):
    monkeypatch.setattr(bridge_mod, "OUTBOX_MAX", 5)
    coord = FlakyCoordinator()
    coord.fail_next = 10 ** 6                                # unreachable for the whole test
    bridge = _attached(coord)

    async def run():
        bridge.attach()
        try:
            for n in range(8):
                bus.emit("ui:needs_refresh", {"n": n})
            bus.emit("notification:new", {"n": "big", "body": "x" * (bridge_mod.PAYLOAD_MAX_BYTES + 1)})
            await asyncio.sleep(0.05)
            assert bridge.status()["outbox"] == 5 and bridge.dropped == 3 and bridge.oversized == 1
            # the outage lasts longer than an event is worth sending
            monkeypatch.setattr(bridge_mod, "OUTBOX_MAX_AGE_S", 0.0)
            coord.fail_next = 0
            await asyncio.sleep(0.1)
            return bridge.status()
        finally:
            bridge.detach()

    status = asyncio.run(run())
    assert status["expired"] == 5 and status["outbox"] == 0 and coord.published == []


def test_bridge_counters_are_part_of_the_cluster_status():
    from core.components.cluster.logic.cluster_service import cluster_service

    assert set(cluster_service.snapshot()["coordinator"]["bridge"]) >= {
        "attached", "published", "relayed", "outbox", "dropped", "expired", "oversized", "failures"}
