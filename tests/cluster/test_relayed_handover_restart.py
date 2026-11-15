"""Work that a peer asks for over the bridge must still be able to talk to the
cluster, and an explicit hand-over must not re-acquire behind its own release.

Regressions from the 1.12 review (opus_review_core):
- H1: a rolling restart requested on a non-leader reaches the leader as a
  relayed event; the run started there inherited the relay mark and every
  ``cluster:restart_instance`` command was dropped by the bridge → nothing
  restarted, each step timed out.
- M2: a lease hand-over requested by a peer (``cluster:lease_handover``)
  released the lease, but the ``cluster:lease_released`` broadcast was dropped
  the same way → peers waited for their next heartbeat.
- M1: a keeper tick during ``release_now``'s wait for the ``lost`` callbacks
  re-acquired the lease; the pending release then freed the row behind the
  keeper's back and a peer held the same lease next to it.
"""
import asyncio

import pytest
from sqlalchemy import select

from cluster_harness import ClusterClock, make_coordinator, make_engine, make_service
from core.bus import bus
from core.components.cluster.logic.bridge import RELAY_ORIGIN, ClusterBridge
from core.components.cluster.logic.leases import RELEASED_TOPIC, LeaseKeeper
from core.components.cluster.logic.models import ClusterEvent
from core.components.cluster.logic.restart import RESTART_INSTANCE_TOPIC, RESTART_REQUEST_TOPIC


@pytest.fixture()
def ready_core(monkeypatch):
    from core.components.boot.logic.boot_service import BootPhase
    from core.services import boot_service, db_instance, vault_instance

    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)


def test_a_restart_forwarded_by_a_peer_sends_its_commands_through_the_bridge(tmp_path, ready_core, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "LYNDRIX_RESTART_INSTANCE_TIMEOUT_SECONDS", 3, raising=False)
    engine = make_engine(tmp_path)

    async def main():
        clock = ClusterClock()
        a = make_service(engine, "pod-a", clock=clock)
        b = make_service(engine, "pod-b", clock=clock)
        await a._start_once()
        await b._start_once()
        await a._heartbeat_once()
        await b._heartbeat_once()
        await a._heartbeat_once()
        assert a.is_leader
        a.bridge.attach()                                   # the real bridge on the leader
        bus.subscribe(RESTART_REQUEST_TOPIC)(a._on_restart_requested)
        try:
            # pod-b forwarded the operator's request; the leader's coordinator relays it
            a.bridge._on_remote_event(RESTART_REQUEST_TOPIC, {"requested_by": "admin"}, "pod-b")
            for _ in range(40):
                await asyncio.sleep(0.05)
                await a.bridge.flush()
                with engine.connect() as conn:
                    rows = list(conn.execute(select(ClusterEvent.topic, ClusterEvent.payload)))
                if any(topic == RESTART_INSTANCE_TOPIC for topic, _ in rows):
                    break
            assert a._restart_task is not None
            a._restart_task.cancel()
        finally:
            bus.unsubscribe(RESTART_REQUEST_TOPIC, a._on_restart_requested)
            a.bridge.detach()
        return rows

    rows = asyncio.run(main())
    commands = [payload for topic, payload in rows if topic == RESTART_INSTANCE_TOPIC]
    assert commands, "the leader's restart command never left the instance"
    assert "pod-b" in str(commands[0])


class RecordingCoordinator:
    driver, instance_id = "fake", "me"

    def __init__(self):
        self.published, self.callbacks, self.released = [], [], []

    async def publish(self, topic, payload):
        self.published.append((topic, payload.get("name")))

    def subscribe(self, callback):
        self.callbacks.append(callback)

    async def try_acquire(self, name, ttl=None):
        return True

    async def release(self, name):
        self.released.append(name)
        return True


def test_a_hand_over_requested_by_a_peer_announces_the_free_lease():
    coord = RecordingCoordinator()
    bridge = ClusterBridge(coord, "me")
    keeper = LeaseKeeper(coord, ttl=30, heartbeat_s=10)

    async def run():
        bridge.attach()
        try:
            keeper.want("job")
            await keeper.tick()
            assert keeper.holds("job")

            async def relayed_handler():                     # what _on_handover_requested runs in
                RELAY_ORIGIN.set("peer-b")
                await keeper.release_now("job")

            await asyncio.create_task(relayed_handler())
            await bridge.flush()
        finally:
            bridge.detach()

    asyncio.run(run())
    assert coord.released == ["job"]
    assert (RELEASED_TOPIC, "job") in coord.published, "peers were not told the lease is free"


def test_a_tick_during_an_explicit_hand_over_does_not_take_the_lease_back(tmp_path):
    async def main():
        engine = make_engine(tmp_path)
        clock = ClusterClock()
        a = make_coordinator(engine, "a", clock=clock)
        b = make_coordinator(engine, "b", clock=clock)
        ka = LeaseKeeper(a, ttl=30, heartbeat_s=10, clock=clock.mono)
        kb = LeaseKeeper(b, ttl=30, heartbeat_s=10, clock=clock.mono)
        events = []

        async def lost():
            events.append("a:lost")
            await asyncio.sleep(0.3)                         # stopping the work takes a moment

        ka.want("job", acquired=lambda: events.append("a:acquired"), lost=lost)
        kb.want("job", acquired=lambda: events.append("b:acquired"))
        await ka.tick()
        await kb.tick()
        assert ka.holds("job") and not kb.holds("job")

        release = asyncio.create_task(ka.release_now("job"))
        await asyncio.sleep(0.05)
        await ka.tick()                                      # A's regular heartbeat tick meanwhile
        assert not ka.holds("job")
        assert await ka.acquire_scoped("job") is False       # nor a one-shot scope
        await release
        assert (await a.holder("job")) is None               # really free at the coordinator
        await kb.tick()                                      # B, kicked by cluster:lease_released
        await ka.tick()
        return ka.holds("job"), kb.holds("job"), events

    a_holds, b_holds, events = asyncio.run(main())
    assert (a_holds, b_holds) == (False, True)
    assert events == ["a:acquired", "a:lost", "b:acquired"]
