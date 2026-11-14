"""Rolling restart of three real (simulated) instances: the leader's driver walks
through them one at a time while the instances really stop, re-register as a new
incarnation and report ready through their heartbeat."""
import asyncio

from cluster_harness import make_service
from core.components.cluster.logic.restart import RollingRestart

BOOT_S = 40


class Rack:
    """Three cluster services on one database. An instance told to restart stops
    gracefully at the next simulated second and boots again ``boot_s`` later."""

    def __init__(self, engine, clock, never_back=()):
        self.engine, self.clock, self.never_back = engine, clock, set(never_back)
        self.services: dict = {}
        self.boot_at: dict = {}
        self.pending_stop: list = []
        self.max_not_ready = 0
        self.restarted: list = []
        self.leader_restart_requested = False

    async def boot(self, iid):
        svc = make_service(self.engine, iid, clock=self.clock)
        await svc._start_once()
        await svc._heartbeat_once()                  # first beat after started=True carries ready
        self.services[iid] = svc
        return svc

    @property
    def leader(self):
        return self.services["pod-2"]

    async def view(self):
        return await self.leader.coordinator.instances()

    def send(self, target):
        self.restarted.append(target.instance_id)
        self.pending_stop.append(target.instance_id)

    def restart_self(self):
        self.leader_restart_requested = True

    async def sleep(self, seconds):
        for iid in self.pending_stop:
            await self.services.pop(iid).stop()
            if iid not in self.never_back:
                self.boot_at[iid] = self.clock.mono.now + BOOT_S
        self.pending_stop.clear()
        self.clock.advance(seconds)
        for iid, at in list(self.boot_at.items()):
            if at <= self.clock.mono.now:
                del self.boot_at[iid]
                await self.boot(iid)
        for svc in list(self.services.values()):
            await svc._heartbeat_once()
        view = await self.view()
        # what a load balancer would see: not ready = not alive+ready, or absent from the registry
        not_ready = {i.instance_id for i in view if not (i.state == "alive" and i.meta.get("ready"))}
        not_ready |= {"pod-1", "pod-2", "pod-3"} - {i.instance_id for i in view}
        self.max_not_ready = max(self.max_not_ready, len(not_ready))

    def driver(self, **kwargs):
        return RollingRestart(leader_id="pod-2", instances=self.view, send=self.send, restart_self=self.restart_self,
                              is_leader=lambda: self.leader.is_leader, requested_by="admin",
                              clock=lambda: self.clock.mono.now, sleep=self.sleep, poll_s=5, **kwargs)


async def _rack(engine, clock):
    rack = Rack(engine, clock)
    await rack.boot("pod-2")                         # first to start → leads
    await rack.boot("pod-1")
    await rack.boot("pod-3")
    assert rack.leader.is_leader
    return rack


def test_rolling_restart_goes_one_at_a_time_and_the_leader_last(engine, clock, ready_core):
    """Given three ready instances, pod-2 leads.
    When the leader drives a rolling restart.
    Then pod-1 and pod-3 are restarted one after the other (never two unavailable at any
    simulated second), each returns as a NEW incarnation that is ready before the next one is
    touched, and the leader restarts itself only after both are back."""

    async def run():
        rack = await _rack(engine, clock)
        before = {i.instance_id: i.incarnation for i in await rack.view()}
        status = await rack.driver().run()
        after = {i.instance_id: i.incarnation for i in await rack.view()}
        return rack, status, before, after

    rack, status, before, after = asyncio.run(run())
    assert status["status"] == "done" and status["done"] == ["pod-1", "pod-3"]
    assert rack.restarted == ["pod-1", "pod-3"] and rack.leader_restart_requested
    assert rack.max_not_ready == 1, "two instances were unavailable at the same time"
    assert after["pod-1"] != before["pod-1"] and after["pod-3"] != before["pod-3"]
    assert after["pod-2"] == before["pod-2"]                      # the leader is still the old process


def test_rolling_restart_aborts_when_an_instance_does_not_come_back(engine, clock, ready_core):
    """Given pod-1 will never come back after its restart.
    When the leader drives a rolling restart (timeout 120 s).
    Then the run aborts naming pod-1, pod-3 and the leader were never touched, and only the
    one instance was unavailable."""

    async def run():
        rack = Rack(engine, clock, never_back={"pod-1"})
        await rack.boot("pod-2")
        await rack.boot("pod-1")
        await rack.boot("pod-3")
        status = await rack.driver(instance_timeout_s=120).run()
        return rack, status

    rack, status = asyncio.run(run())
    assert status["status"] == "aborted" and "pod-1 did not come back ready within 120s" in status["message"]
    assert rack.restarted == ["pod-1"] and not rack.leader_restart_requested
    assert rack.max_not_ready == 1 and rack.leader.is_leader
