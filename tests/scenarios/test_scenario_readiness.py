"""Stories about when an instance may take traffic: plugin convergence after boot,
the SIGTERM drain, and one instance with the wrong ``STORAGE_SECRET``."""
import asyncio

from scenario_kit import Recorder

from cluster_harness import make_service
from core.components.cluster.logic.cluster_service import ClusterService
from core.components.cluster.logic.drain import DrainController
from core.components.plugins.logic.manager import module_manager

TTL, GRACE = 30, 90


def test_new_instance_waits_for_its_plugins_and_a_peer_sees_it_ready_only_then(
        engine, clock, ready_core, monkeypatch):
    """Given a new pod finished booting while its plugins are still being installed
    (``boot_converged`` False, wait cap 60 s).
    When convergence completes, and in a second run when it never does.
    Then it reports ``plugins:converging`` (and a peer sees ``ready: false`` in its row) until
    the flag flips or the cap elapsed, then it is ready and the peer sees ``ready: true``."""
    monkeypatch.setattr(module_manager, "boot_converged", False, raising=False)
    old, new = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def peer_sees_ready():
        await old._heartbeat_once()
        return next(i for i in old._instances if i.instance_id == "pod-2").meta["ready"]

    async def run():
        await old._start_once()
        await new._start_once()
        new.plugin_wait_s = 60.0
        new._boot_complete_at = clock.mono.now
        await new._heartbeat_once()
        assert new.readiness() == (False, ["plugins:converging"]) and not await peer_sees_ready()

        clock.advance(20)
        await new._heartbeat_once()
        assert new.readiness() == (False, ["plugins:converging"])
        monkeypatch.setattr(module_manager, "boot_converged", True, raising=False)    # the pass finished
        assert new.readiness() == (True, [])
        await new._heartbeat_once()
        assert await peer_sees_ready()

        # A pass that never finishes must not keep the instance out of the load balancer forever.
        monkeypatch.setattr(module_manager, "boot_converged", False, raising=False)
        new._boot_complete_at = clock.mono.now
        assert new.readiness() == (False, ["plugins:converging"])
        for _ in range(7):
            clock.advance(10)
            await new._heartbeat_once()
        assert new.readiness() == (True, [])

    asyncio.run(run())


def test_sigterm_drain_with_three_instances(engine, clock, ready_core, lost_by):
    """Given pod-1 leads and holds a worker lease; pod-2 and pod-3 stand by.
    When pod-1 receives SIGTERM (drain of 1 s).
    Then readiness is ``draining`` at once, peers see ``draining`` after one beat, the worker
    stops (``lost``) and only then the leases are released, one peer becomes leader while pod-1
    still drains, the row ends as a tombstone (``restarting`` for both observers, no loss
    announced); after the grace period each observer announces the loss exactly once."""
    rec, job = Recorder(clock), "monitoring:prober"
    seen = {"pod-2": lost_by("pod-2"), "pod-3": lost_by("pod-3")}
    p1, p2, p3 = (make_service(engine, f"pod-{i}", clock=clock) for i in (1, 2, 3))
    order: list = []

    async def run():
        rec.watch(p1, job)
        for svc in (p1, p2, p3):
            await svc._start_once()
        await p1.leases.tick()
        assert p1.is_leader and p1.leases.holds(job) and p1.readiness() == (True, [])

        release = p1.coordinator.release

        async def recording_release(name):
            order.append(("release", name, len([e for e in rec.log if e[1] == "lost"])))
            return await release(name)

        p1.coordinator.release = recording_release
        drain = DrainController(seconds=1, on_drain=p1._on_drain, on_drain_start=p1.begin_drain)
        drain._forward = lambda: order.append("forwarded")      # do not re-raise SIGTERM inside the test
        drain._on_sigterm()
        assert p1.readiness()[1] == ["draining"]                # immediately, not after the sleep
        await asyncio.sleep(0.3)
        for peer in (p2, p3):
            await peer._heartbeat_once()
            assert peer.instance_state("pod-1")["state"] == "draining"
        assert p1.leases.held == [] and not p1.is_leader
        released_job = [o for o in order if isinstance(o, tuple) and o[1] == job]
        assert released_job and released_job[0][2] >= 1          # the worker's ``lost`` ran before its row was freed
        await p2.leases.tick()
        await p3.leases.tick()
        assert [p2.is_leader, p3.is_leader].count(True) == 1    # exactly one successor, during the drain

        await asyncio.sleep(1.2)                                # drain over: tombstone, then the process exits
        for peer in (p2, p3):
            await peer._heartbeat_once()
            assert peer.instance_state("pod-1")["state"] == "restarting"
        assert order[-1] == "forwarded"
        assert seen["pod-2"] == seen["pod-3"] == []

        for _ in range(GRACE // 10 + 1):                        # nobody comes back
            clock.advance(10)
            await p2._heartbeat_once()
            await p3._heartbeat_once()
        for observer, events in seen.items():
            assert [e["instance_id"] for e in events] == ["pod-1"], observer
            assert events[0]["graceful"] is True

    asyncio.run(run())
    # the worker was told to stop before any lease row was freed
    assert rec.events(job) == [("pod-1", "acquired"), ("pod-1", "lost")]


def test_a_wrong_storage_secret_takes_only_the_odd_instance_out_of_rotation(engine, clock, ready_core):
    """Given two healthy instances and a third that joined with a different STORAGE_SECRET.
    When the preflight runs everywhere.
    Then the two healthy instances stay ready (a single odd joiner must not cause a
    cluster-wide outage) and only the odd one reports ``preflight:storage_secret``; its
    heartbeat row tells the peers it is not ready."""
    healthy = [make_service(engine, f"pod-{i}", clock=clock) for i in (1, 2)]
    odd = make_service(engine, "pod-3", clock=clock)
    odd.__class__ = type("OddSecretService", (ClusterService,), {"secret_fingerprint": "fp-of-another-secret"})
    svcs = [*healthy, odd]

    async def run():
        for svc in svcs:
            await svc._start_once()
        for _ in range(2):                                      # first sweep writes the fingerprints, second reads them
            for svc in svcs:
                await svc._heartbeat_once()
        for svc in svcs:
            await svc.run_preflight()
            await svc._heartbeat_once()
        for svc in healthy:
            assert svc.readiness() == (True, []), svc.instance_id
        assert odd.readiness() == (False, ["preflight:storage_secret"])
        await healthy[0]._heartbeat_once()                      # a peer reads the odd one's latest row
        row = next(i for i in healthy[0]._instances if i.instance_id == "pod-3")
        assert row.meta["ready"] is False and row.meta["reasons"] == ["preflight:storage_secret"]

    asyncio.run(run())
