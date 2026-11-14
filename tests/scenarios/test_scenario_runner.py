"""Stories about the ``runner`` role (the IaC orchestrator's executor) coming
and going while two API instances watch it — the mechanism behind "No runner
instance online" after a rollout."""
import asyncio

from scenario_kit import Recorder

from cluster_harness import make_service

TTL, GRACE = 30, 90


async def _runner(engine, clock, iid="docker-auto-runner"):
    runner = make_service(engine, iid, clock=clock, roles=["runner"])
    await runner._start_once()
    return runner


async def _beat(*services):
    for svc in services:
        await svc._heartbeat_once()


async def _pass(clock, seconds, *services, step=10):
    """Let time pass while the given (living) instances keep heart-beating."""
    for _ in range(int(seconds // step)):
        clock.advance(step)
        await _beat(*services)


def test_runner_restart_then_grace_expiry_then_return_then_crash(engine, clock, ready_core, lost_by):
    """Given pod-1 and pod-2 watch the only ``runner`` instance.
    When it stops gracefully, stays away beyond the restart grace, comes back as a new
    incarnation and later crashes.
    Then within the grace both observers say ``restarting`` (``has_live_role`` stays true,
    no ``cluster:instance_lost``); after the grace the role is ``dead`` and each observer
    announces the loss exactly once; the new incarnation is ``alive`` again and its later
    crash is announced again (once more per observer)."""
    seen = {"pod-1": lost_by("pod-1"), "pod-2": lost_by("pod-2")}
    p1, p2 = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)

    async def run():
        await p1._start_once()
        await p2._start_once()
        runner = await _runner(engine, clock)
        await _beat(p1, p2)
        first = p1.instance_state("docker-auto-runner")["incarnation"]
        assert p1.role_state("runner")["state"] == p2.role_state("runner")["state"] == "alive"

        await runner.stop()                                         # compose recreate: graceful stop
        await _beat(p1, p2)
        for observer in (p1, p2):
            assert observer.role_state("runner")["state"] == "restarting"
            assert observer.role_state("runner")["state"] != "dead"            # has_live_role is True
        assert seen["pod-1"] == seen["pod-2"] == []

        await _pass(clock, GRACE - 10, p1, p2)
        assert p1.role_state("runner")["state"] == "restarting" and seen["pod-1"] == []

        await _pass(clock, 20, p1, p2)                              # grace over, nobody came back
        await _beat(p1, p2)                                         # a second beat must not announce again
        for observer in (p1, p2):
            assert observer.role_state("runner")["state"] == "dead"
        assert [e["instance_id"] for e in seen["pod-1"]] == ["docker-auto-runner"]
        assert [e["instance_id"] for e in seen["pod-2"]] == ["docker-auto-runner"]
        assert seen["pod-1"][0]["graceful"] is True

        runner = await _runner(engine, clock)                       # the new container registers
        await _beat(p1, p2)
        state = p1.instance_state("docker-auto-runner")
        assert state["state"] == "alive" and state["incarnation"] not in (None, first)
        assert p2.role_state("runner")["state"] == "alive"

        await _pass(clock, TTL + 10, p1, p2)                        # crash: silence
        await _beat(p1, p2)
        assert p1.role_state("runner")["state"] == "dead"
        assert [e["graceful"] for e in seen["pod-1"]] == [True, False]
        assert [e["graceful"] for e in seen["pod-2"]] == [True, False]

    asyncio.run(run())


def test_a_second_process_with_the_runner_id_makes_the_older_one_stand_back(engine, clock, ready_core, lost_by):
    """Given the runner process is still alive while a new container starts under the same
    ``LYNDRIX_INSTANCE_ID`` (compose recreate that did not stop the old one in time).
    When both beat and contend for the runner lease.
    Then the older process reports ``instance_id_conflict`` and stops acting as holder
    (``lost``), the newer one holds the lease, the observers see ONE alive runner with the
    new incarnation and no ``cluster:instance_lost``."""
    rec, work = Recorder(clock), "iac:runner_queue"
    seen = lost_by("pod-1")
    p1 = make_service(engine, "pod-1", clock=clock)

    async def run():
        await p1._start_once()
        old = make_service(engine, "docker-auto-runner", clock=clock, roles=["runner"])
        new = make_service(engine, "docker-auto-runner", clock=clock, roles=["runner"])
        rec.watch(old, work)
        await old._start_once()
        await old.leases.tick()
        assert old.leases.holds(work) and old.readiness() == (True, [])
        clock.advance(5)
        await new._start_once()
        clock.advance(5)
        await old._heartbeat_once()                                 # the older one notices
        assert old.readiness() == (False, ["instance_id_conflict"])
        assert old.leases.held == []
        await _beat(new, p1)
        assert p1.instance_state("docker-auto-runner")["incarnation"] == new.incarnation
        assert p1.role_state("runner")["state"] == "alive" and seen == []
        assert new.readiness() == (True, [])

    asyncio.run(run())
    assert rec.events(work) == [("docker-auto-runner", "acquired"), ("docker-auto-runner", "lost")]
