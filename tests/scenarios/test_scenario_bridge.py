"""Stories about events travelling between 3 instances through the shared
database. pod-1 is the one whose bridge is attached to the (process-global) bus;
pod-2 and pod-3 are observers that read the event table with their own pollers
(real 100 ms poll loops, hence the short real waits)."""
import asyncio
from datetime import datetime

from scenario_kit import eventually
from sqlalchemy import insert

from cluster_harness import make_coordinator, make_service
from core.bus import bus
from core.components.cluster.logic import bridge as bridge_mod
from core.components.cluster.logic.bridge import ORIGIN_KEY
from core.components.cluster.logic.models import ClusterEvent

TOPIC = "ui:needs_refresh"


class Trio:
    """pod-1 (bridge attached) + two observers, with what each of them received."""

    def __init__(self, engine, clock):
        self.a = make_service(engine, "pod-1", clock=clock)
        self.b = make_coordinator(engine, "pod-2", clock=clock)
        self.c = make_coordinator(engine, "pod-3", clock=clock)
        self.got = {"pod-2": [], "pod-3": []}
        self.local: list = []                    # what pod-1's own bus listeners saw
        for coord in (self.b, self.c):
            coord.subscribe(lambda topic, payload, origin, who=coord.instance_id:
                            self.got[who].append((topic, payload.get("n"), origin)))
        self.listener = lambda payload: self.local.append(payload)

    async def __aenter__(self):
        await self.b.start()
        await self.c.start()
        await self.a._start_once()
        self.a.bridge.retry_delays = (0.05,)
        self.a.bridge.attach()
        bus.subscribe(TOPIC)(self.listener)
        return self

    async def __aexit__(self, *exc):
        self.a.bridge.detach()
        bus.unsubscribe(TOPIC, self.listener)
        for coord in (self.b, self.c):
            await coord.stop()

    def seen_by_both(self, count: int) -> bool:
        return all(len(rows) >= count for rows in self.got.values())


def test_an_event_reaches_every_peer_exactly_once_and_never_echoes_back(engine, clock, ready_core):
    """Given pod-1 publishes through its bridge, pod-2 and pod-3 poll the table.
    When pod-1 emits one cluster event.
    Then pod-2 and pod-3 each receive it exactly once with origin pod-1, pod-1's own
    listeners saw only the original (no ``_origin``, no echo), and its bridge relayed nothing."""

    async def run():
        async with Trio(engine, clock) as trio:
            bus.emit(TOPIC, {"n": 1})
            assert await eventually(lambda: trio.seen_by_both(1))
            await asyncio.sleep(0.4)                  # several more polls: a duplicate would show now
            return trio

    trio = asyncio.run(run())
    assert trio.got == {"pod-2": [(TOPIC, 1, "pod-1")], "pod-3": [(TOPIC, 1, "pod-1")]}
    assert trio.local == [{"n": 1}] and trio.a.bridge.relayed == 0 and trio.a.coordinator.events_received == 0


def test_events_during_a_database_outage_arrive_in_order_after_the_heal(engine, clock, ready_core):
    """Given pod-1's database is unreachable.
    When pod-1 emits five events in a row and the database comes back.
    Then nothing was delivered meanwhile, the bridge counted the failures, and afterwards
    pod-2 and pod-3 receive 1..5 in publish order, nothing dropped."""

    async def run():
        async with Trio(engine, clock) as trio:
            trio.a.coordinator.fail = ConnectionError("db down")
            for n in range(1, 6):
                bus.emit(TOPIC, {"n": n})
            assert await eventually(lambda: trio.a.bridge.failures >= 1)
            assert trio.got == {"pod-2": [], "pod-3": []} and trio.a.bridge.status()["outbox"] == 5
            trio.a.coordinator.fail = None
            assert await trio.a.bridge.flush(timeout=3)
            assert await eventually(lambda: trio.seen_by_both(5))
            return trio

    trio = asyncio.run(run())
    for who in ("pod-2", "pod-3"):
        assert [n for _, n, _ in trio.got[who]] == [1, 2, 3, 4, 5], who
    assert trio.a.bridge.dropped == trio.a.bridge.expired == 0


def test_a_long_outage_drops_the_oldest_events_and_counts_them(engine, clock, ready_core, monkeypatch):
    """Given the outbox holds at most 3 events and the database is down.
    When pod-1 emits six events and the database heals.
    Then peers get a gap-free ordered suffix, and delivered + dropped == emitted — the loss
    is visible in ``bridge.dropped`` (shown on the Cluster page), not silent."""
    monkeypatch.setattr(bridge_mod, "OUTBOX_MAX", 3)

    async def run():
        async with Trio(engine, clock) as trio:
            trio.a.coordinator.fail = ConnectionError("db down")
            for n in range(1, 7):
                bus.emit(TOPIC, {"n": n})
            assert await eventually(lambda: trio.a.bridge.dropped >= 3)
            trio.a.coordinator.fail = None
            assert await trio.a.bridge.flush(timeout=3)
            await eventually(lambda: trio.seen_by_both(3))
            await asyncio.sleep(0.3)
            return trio

    trio = asyncio.run(run())
    received = [n for _, n, _ in trio.got["pod-2"]]
    assert received == sorted(received) and received[-3:] == [4, 5, 6]
    assert len(received) + trio.a.bridge.dropped == 6
    assert [n for _, n, _ in trio.got["pod-3"]] == received


def test_a_late_committed_lower_id_is_still_delivered_once(engine, clock, ready_core):
    """Given pod-3 commits event 1000, pod-1 and pod-2 read it, and pod-1's slower
    transaction commits event 999 afterwards (ids are handed out before commit).
    When the pollers look again.
    Then pod-1's bridge relays BOTH events to its local bus, each exactly once (the
    cursor does not skip the late one), and pod-2 gets both as well."""

    def commit(event_id, origin, n):
        with engine.begin() as conn:
            conn.execute(insert(ClusterEvent).values(id=event_id, topic=TOPIC, payload={"n": n}, origin=origin,
                                                     created_at=datetime.utcnow()))

    async def run():
        async with Trio(engine, clock) as trio:
            commit(1000, "pod-3", 1)
            assert await eventually(lambda: len(trio.local) >= 1 and len(trio.got["pod-2"]) >= 1)
            commit(999, "pod-4", 2)                  # the late commit
            assert await eventually(lambda: len(trio.local) >= 2 and len(trio.got["pod-2"]) >= 2)
            await asyncio.sleep(0.4)
            return trio

    trio = asyncio.run(run())
    assert trio.local == [{"n": 1, ORIGIN_KEY: "pod-3"}, {"n": 2, ORIGIN_KEY: "pod-4"}]
    assert [n for _, n, _ in trio.got["pod-2"]] == [1, 2]
    assert trio.a.coordinator.events_late == 1


def test_oversized_and_secret_looking_payloads_never_reach_the_peers(engine, clock, ready_core):
    """Given a healthy bridge.
    When an event with a 70 kB payload and an event with a secret-looking field are emitted,
    followed by a normal event.
    Then the oversized one is refused (counted), the secret one stays on pod-1 (its local
    listeners still receive it), the peers only ever see the normal event."""

    async def run():
        async with Trio(engine, clock) as trio:
            bus.emit(TOPIC, {"n": "big", "blob": "x" * (bridge_mod.PAYLOAD_MAX_BYTES + 1)})
            bus.emit(TOPIC, {"n": "secret", "password": "dummy-token-not-real"})
            bus.emit(TOPIC, {"n": "normal"})
            assert await eventually(lambda: trio.seen_by_both(1))
            await asyncio.sleep(0.3)
            return trio

    trio = asyncio.run(run())
    assert trio.got == {"pod-2": [(TOPIC, "normal", "pod-1")], "pod-3": [(TOPIC, "normal", "pod-1")]}
    assert trio.a.bridge.oversized == 1
    assert sorted(str(p["n"]) for p in trio.local if "password" in p or p["n"] == "secret") == ["secret"]
    assert len(trio.local) == 3
