"""Stories about who leads: a graceful hand-over, a crashed leader that comes
back, a paused one, and instances that contend at the same instant.

Every story runs 2-3 simulated instances (one ``ClusterService`` each) against
one file database and one fake clock — no sleeping, no real time.
"""
import asyncio

from scenario_kit import Recorder

from cluster_harness import make_service
from core.components.cluster.logic.cluster_service import LEADER_LEASE

TTL = 30


def test_graceful_leader_stop_hands_over_at_once_and_lost_comes_first(engine, clock, ready_core):
    """Given pod-1 leads and pod-2 stands by.
    When pod-1 stops gracefully (rollout, SIGTERM).
    Then pod-2 holds ``core:leader`` without any time passing (no TTL wait), pod-1's
    ``lost`` fired BEFORE pod-2's ``acquired``, and pod-2's token is the larger one."""
    rec = Recorder(clock)
    a, b = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def run():
        rec.watch(a, LEADER_LEASE), rec.watch(b, LEADER_LEASE)
        await a._start_once()
        await b._start_once()
        assert a.is_leader and not b.is_leader
        before = clock.mono.now
        await a.stop()
        await b.leases.tick()                       # what the release broadcast triggers on a peer
        assert clock.mono.now == before and b.is_leader
        await b._heartbeat_once()
        assert b.leader_id == "pod-2" and not a.is_leader and a.leases.held == []
        assert (await b.coordinator.holder(LEADER_LEASE)).holder == "pod-2"

    asyncio.run(run())
    assert rec.events(LEADER_LEASE) == [("pod-1", "acquired"), ("pod-1", "lost"), ("pod-2", "acquired")]
    assert rec.tokens[("pod-2", LEADER_LEASE)] > rec.tokens[("pod-1", LEADER_LEASE)]


def test_crashed_leader_is_replaced_after_the_ttl_and_cannot_act_when_it_returns(engine, clock, ready_core, lost_by):
    """Given pod-1 leads and then dies without a goodbye (no stop, no heartbeat).
    When pod-2 tries before the TTL, then after it.
    Then pod-2 takes over only after the TTL with a larger token; pod-2 announces
    pod-1 as lost exactly once; and when the old process wakes up and ticks it
    fences itself (``lost``), fails to re-acquire and leaves leadership to pod-2."""
    rec = Recorder(clock)
    seen_by_b = lost_by("pod-2")
    a, b = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def run():
        rec.watch(a, LEADER_LEASE), rec.watch(b, LEADER_LEASE)
        await a._start_once()
        await b._start_once()
        old_token = rec.tokens[("pod-1", LEADER_LEASE)]

        clock.advance(TTL - 1)                      # pod-1 is silent, its row has one second left
        await b.leases.tick()
        assert not b.is_leader and (await b.coordinator.holder(LEADER_LEASE)).holder == "pod-1"

        clock.advance(2)                            # TTL over
        await b._heartbeat_once()
        await b.leases.tick()
        await b._heartbeat_once()
        assert b.is_leader and b.leader_id == "pod-2"
        assert b.instance_state("pod-1")["state"] == "dead"
        assert [e["instance_id"] for e in seen_by_b] == ["pod-1"]       # announced once, not per beat
        assert rec.tokens[("pod-2", LEADER_LEASE)] > old_token

        # The old process resumes (it was only paused/partitioned, not gone).
        assert not a.leases.holds(LEADER_LEASE)     # its own deadline passed long ago
        await a.leases.tick()
        await a._heartbeat_once()
        assert not a.is_leader and a.leases.held == [] and a.leases.token(LEADER_LEASE) is None
        assert a.leader_id == "pod-2" and b.is_leader
        assert (await b.coordinator.holder(LEADER_LEASE)).holder == "pod-2"

    asyncio.run(run())
    assert rec.events(LEADER_LEASE) == [("pod-1", "acquired"), ("pod-2", "acquired"), ("pod-1", "lost")]


def test_paused_instance_fences_itself_before_it_renews(engine, clock, ready_core):
    """Given pod-1 holds a worker lease and its process is paused (``docker pause``:
    the loop stands still, monotonic and database clocks keep running) beyond the TTL.
    When pod-2 takes the lease and pod-1 resumes.
    Then ``holds()`` is already False on wake-up, ``lost`` fires BEFORE pod-1 makes any
    renewal call, the renewal is refused, and pod-1's old token is lower than pod-2's."""
    rec, job = Recorder(clock), "monitoring:prober"
    a, b = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def run():
        rec.watch(a, job), rec.watch(b, job)
        await a._start_once()
        await b._start_once()
        await a.leases.tick()
        assert a.leases.holds(job)
        renewals = a.coordinator.acquire

        async def recording_acquire(*args, **kwargs):
            rec.log.append(("pod-1", "renewal-call", job))
            return await renewals(*args, **kwargs)

        a.coordinator.acquire = recording_acquire

        clock.advance(TTL + 10)                     # pod-1 sleeps through it all
        await b.leases.tick()
        assert b.leases.holds(job)
        assert not a.leases.holds(job)              # the guard a plugin checks is deadline-aware
        await a.leases.tick()
        assert not a.leases.holds(job) and a.leases.token(job) is None

    asyncio.run(run())
    assert rec.index("pod-1", "lost", job) < rec.index("pod-1", "renewal-call", job)
    assert rec.index("pod-1", "lost", job) < rec.index("pod-2", "acquired", job)
    assert rec.tokens[("pod-2", job)] > rec.tokens[("pod-1", job)]


def test_frozen_monotonic_clock_is_caught_by_the_database_at_the_first_renewal(engine, clock, ready_core):
    """Given pod-1 holds a lease and its MONOTONIC clock stood still (VM suspend) while
    the database clock ran past the TTL; pod-2 took the lease meanwhile.
    When pod-1 wakes up.
    Then its local deadline cannot tell (documented limit: it still believes it holds until
    its first renewal round), but that first round is refused by the database, ``lost``
    fires, and the stale token is lower than the new holder's — so a downstream that checks
    the token rejects pod-1's late writes."""
    rec, job = Recorder(clock), "iac:webhook_sync"
    a, b = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def run():
        rec.watch(a, job), rec.watch(b, job)
        await a._start_once()
        await b._start_once()
        await a.leases.tick()
        stale = a.leases.token(job)
        clock.db.advance(TTL + 10)                  # only the database clock moved
        await b.leases.tick()
        assert b.leases.token(job) > stale
        assert a.leases.holds(job)                  # the gap: bounded by one renewal round
        await a.leases.tick()
        assert not a.leases.holds(job)

    asyncio.run(run())
    assert rec.events(job) == [("pod-1", "acquired"), ("pod-2", "acquired"), ("pod-1", "lost")]


def test_instances_contending_at_the_same_instant_produce_exactly_one_holder(engine, clock, ready_core):
    """Given three instances that all want twelve free leases.
    When every keeper runs its acquire round concurrently, twice.
    Then for every lease exactly one instance got ``acquired``, the database names that
    one as holder, and nobody got ``lost``."""
    rec = Recorder(clock)
    names = [f"scenario:job-{i}" for i in range(12)]
    svcs = [make_service(engine, f"pod-{i}", clock=clock) for i in (1, 2, 3)]

    async def run():
        for svc in svcs:
            await svc._start_once()
            for name in names:
                rec.watch(svc, name)
        for _ in range(2):
            await asyncio.gather(*(svc.leases.tick() for svc in svcs))
        holders = {lease.name: lease.holder for lease in await svcs[0].coordinator.leases()}
        return holders

    holders = asyncio.run(run())
    for name in names:
        acquired = [who for who, event in rec.events(name) if event == "acquired"]
        assert len(acquired) == 1, f"{name}: {acquired}"
        assert holders[name] == acquired[0]
    assert not [entry for entry in rec.log if entry[1] == "lost"]
