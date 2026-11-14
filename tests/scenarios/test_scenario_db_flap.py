"""Stories about a flapping or hanging database as seen by 2 instances.

``svc.coordinator.fail`` / ``.delay`` (``FaultyDbCoordinator``) break the
database per instance; the fake clock models the passing of time.
"""
import asyncio
import time

from scenario_kit import Recorder

from cluster_harness import make_service

TTL = 30
JOB = "scenario:worker"


def test_a_short_database_flap_costs_nobody_a_lease_and_readiness_heals_itself(engine, clock, ready_core):
    """Given pod-1 holds the leader lease and a worker lease, pod-2 stands by.
    When pod-1's database is unreachable for 12 s (< TTL 30 s, < fencing deadline 22.5 s).
    Then no ``lost`` fires, pod-2 cannot take anything, the failure is counted and
    readiness stays green; after the heal the next beat clears the counter and the
    leases are renewed under the SAME token."""
    rec = Recorder(clock)
    a, b = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def run():
        rec.watch(a, JOB), rec.watch(b, JOB)
        await a._start_once()
        await b._start_once()
        await a.leases.tick()
        token = a.leases.token(JOB)

        a.coordinator.fail = ConnectionError("(2013, 'Lost connection to MySQL server during query')")
        clock.advance(12)
        await a._heartbeat_once()
        await a.leases.tick()
        await b.leases.tick()
        assert a.leases.holds(JOB) and a.is_leader and not b.leases.holds(JOB)
        assert a.heartbeat_status()["failures"] == 1 and "Lost connection" in a.heartbeat_status()["last_error"]
        assert a.readiness() == (True, [])           # the beat is late, not yet stale

        a.coordinator.fail = None
        await a._heartbeat_once()
        await a.leases.tick()
        assert a.heartbeat_status()["failures"] == 0 and a.readiness() == (True, [])
        assert a.leases.token(JOB) == token and a.leases.holds(JOB)
        assert (await b.coordinator.holder(JOB)).holder == "pod-1"

    asyncio.run(run())
    assert [entry for entry in rec.log if entry[1] == "lost"] == []


def test_an_outage_longer_than_the_fencing_margin_loses_the_lease_before_a_peer_can_take_it(
        engine, clock, ready_core):
    """Given pod-1 holds a worker lease (TTL 30 s, fencing deadline 22.5 s).
    When its database is down for 31 s, then heals.
    Then ``lost`` fires at 23 s while the row still belongs to pod-1 for the peers, pod-2
    can only take it after the TTL, readiness shows ``heartbeat`` while the outage lasts
    and nothing after it, and after the heal exactly one instance holds the lease."""
    rec = Recorder(clock)
    a, b = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def run():
        rec.watch(a, JOB), rec.watch(b, JOB)
        await a._start_once()
        await b._start_once()
        await a.leases.tick()
        started = clock.mono.now

        a.coordinator.fail = ConnectionError("db down")
        clock.advance(23)
        a.leases.fence_expired()                    # the watchdog's job at the deadline
        await b.leases.tick()
        assert not a.leases.holds(JOB) and not b.leases.holds(JOB)      # fenced, but not yet takeable
        assert (await b.coordinator.holder(JOB)).holder == "pod-1"

        clock.advance(8)                            # 31 s: the row has expired for everybody
        await a._heartbeat_once()
        assert "heartbeat" in a.readiness()[1]
        await b.leases.tick()
        assert b.leases.holds(JOB)

        a.coordinator.fail = None
        await a._heartbeat_once()
        await a.leases.tick()
        assert a.readiness() == (True, [])
        assert not a.leases.holds(JOB) and b.leases.holds(JOB)
        holders = [lease.holder for lease in await a.coordinator.leases() if lease.name == JOB]
        assert holders == ["pod-2"]
        return started

    started = asyncio.run(run())
    assert rec.events(JOB) == [("pod-1", "acquired"), ("pod-1", "lost"), ("pod-2", "acquired")]
    assert rec.at[("pod-1", "lost", JOB)] - started == 23
    assert rec.at[("pod-2", "acquired", JOB)] - started >= TTL


def test_a_hung_database_call_is_bounded_and_a_late_answer_does_not_resurrect_the_lease(engine, clock, ready_core):
    """Given pod-1 holds a lease (short timings: TTL 1.2 s, heartbeat 0.4 s) and its database
    calls start to hang (``delay`` > TTL), the calls running in worker threads.
    When it beats and renews.
    Then the heartbeat gives up after one TTL (not minutes), the instance turns unready
    once the beat is stale, its lease is fenced, pod-2 takes it after the TTL, and when
    the late calls finally return pod-1 neither holds the lease nor was told ``acquired``."""
    rec = Recorder(clock)
    kwargs = dict(clock=clock, ttl=1.2, heartbeat_s=0.4, grace_s=5)
    a, b = make_service(engine, "pod-1", **kwargs), make_service(engine, "pod-2", **kwargs)

    async def run():
        rec.watch(a, JOB), rec.watch(b, JOB)
        await a._start_once()
        await b._start_once()
        await a.leases.tick()
        assert a.leases.holds(JOB)

        a.coordinator.delay = 1.6
        real = time.monotonic()
        await a._heartbeat_once()
        assert time.monotonic() - real < 1.5        # bounded by the TTL, not by the hang
        assert a.heartbeat_status()["failures"] == 1 and "timed out" in a.heartbeat_status()["last_error"]
        await a.leases.tick()                       # the renewal hangs as well; the round gives up
        assert a.leases.holds(JOB)

        clock.advance(0.7)                          # past the fencing deadline (1.2 - 0.6)
        a.leases.fence_expired()
        await b.leases.tick()
        assert not a.leases.holds(JOB) and not b.leases.holds(JOB)
        clock.advance(0.6)                          # past the TTL
        await b.leases.tick()
        assert b.leases.holds(JOB) and "heartbeat" in a.readiness()[1]

        await asyncio.sleep(1.5)                    # the hung calls return now
        a.coordinator.delay = 0
        await a.leases.tick()
        await a._heartbeat_once()
        assert not a.leases.holds(JOB) and b.leases.holds(JOB)
        assert a.readiness() == (True, [])
        assert [lease.holder for lease in await b.coordinator.leases() if lease.name == JOB] == ["pod-2"]

    asyncio.run(run())
    assert rec.events(JOB) == [("pod-1", "acquired"), ("pod-1", "lost"), ("pod-2", "acquired")]
