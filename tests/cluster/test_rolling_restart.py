"""CL-12: the cluster "rolling restart" restarts one instance at a time, waits
for it to be ready again, puts the leader last, leaves runners alone unless
asked, and aborts when an instance does not come back."""
import asyncio
from datetime import datetime

import pytest

import core.services  # noqa: F401  — before core.api (circular import)
from core.bus import bus
from core.components.cluster.logic.cluster_service import ClusterService
from core.components.cluster.logic.coordinator import InstanceInfo
from core.components.cluster.logic.restart import (
    RESTART_INSTANCE_TOPIC,
    RESTART_REQUEST_TOPIC,
    RestartRefused,
    RollingRestart,
    plan,
)

NOW = datetime(2026, 11, 14, 12, 0, 0)


def _inst(iid, roles=("api", "leader"), *, incarnation=None, api="1.14.0", state="alive", ready=True, host=None):
    return InstanceInfo(iid, host or iid, list(roles), "1.12.0", api, NOW, NOW, 1.0, state in ("alive", "draining"),
                        False, {"ready": ready}, state=state, incarnation=incarnation or f"{iid}-1")


class World:
    """A simulated cluster on a simulated clock: an instance told to restart is
    gone for ``boot_s`` seconds and comes back as a new incarnation."""

    def __init__(self, instances, boot_s=40.0, never_back=()):
        self.t = 0.0
        self.instances = {i.instance_id: i for i in instances}
        self.boot_s, self.never_back = boot_s, set(never_back)
        self.back_at: dict = {}
        self.restarted: list = []
        self.max_not_ready = 0
        self.leader_restarted_at = None
        self.is_leader = True

    def _observe(self):
        not_ready = [i for i in self.instances.values() if not (i.state == "alive" and i.meta.get("ready"))]
        self.max_not_ready = max(self.max_not_ready, len(not_ready))

    async def view(self):
        return list(self.instances.values())

    def send(self, target):
        self.restarted.append(target.instance_id)
        self.instances[target.instance_id] = _inst(target.instance_id, target.roles, incarnation=target.incarnation,
                                                   state="restarting", ready=False)
        if target.instance_id not in self.never_back:
            self.back_at[target.instance_id] = self.t + self.boot_s
        self._observe()

    async def sleep(self, seconds):
        self.t += seconds
        for iid, at in list(self.back_at.items()):
            if at <= self.t:
                old = self.instances[iid]
                generation = int(old.incarnation.rsplit("-", 1)[1]) + 1
                self.instances[iid] = _inst(iid, old.roles, incarnation=f"{iid}-{generation}")
                del self.back_at[iid]
        self._observe()

    def restart_self(self):
        self.leader_restarted_at = self.t
        self.restarted.append("LEADER")

    def run(self, leader="pod-2", **kwargs):
        driver = RollingRestart(leader_id=leader, instances=self.view, send=self.send, restart_self=self.restart_self,
                                is_leader=lambda: self.is_leader, requested_by="admin", clock=lambda: self.t,
                                sleep=self.sleep, **kwargs)
        return asyncio.run(driver.run())


def _prod():
    return [_inst("pod-1"), _inst("pod-2"), _inst("pod-3"), _inst("pod-4"), _inst("docker-auto", ["api"]),
            _inst("docker-auto-runner", ["runner"])]


def test_one_instance_at_a_time_leader_last_runner_untouched():
    world = World(_prod())
    status = world.run()
    assert status["status"] == "done"
    assert world.restarted == ["docker-auto", "pod-1", "pod-3", "pod-4", "LEADER"]     # pod-2 leads → last
    assert world.max_not_ready == 1, "more than one instance was down at the same time"
    assert status["done"] == ["docker-auto", "pod-1", "pod-3", "pod-4"]
    assert "runner role" in status["skipped"]["docker-auto-runner"]
    assert world.leader_restarted_at >= 4 * world.boot_s                               # after everyone was back
    assert {i.incarnation for i in world.instances.values() if i.instance_id.startswith("pod-")} == {
        "pod-1-2", "pod-2-1", "pod-3-2", "pod-4-2"}


def test_runners_are_restarted_on_request_only():
    world = World(_prod())
    status = world.run(include_runners=True)
    assert "docker-auto-runner" in world.restarted and status["skipped"] == {}
    assert world.max_not_ready == 1


def test_an_instance_that_does_not_come_back_aborts_the_run():
    world = World(_prod(), never_back={"pod-1"})
    status = world.run(instance_timeout_s=120)
    assert status["status"] == "aborted" and "pod-1 did not come back ready within 120s" in status["message"]
    assert world.restarted == ["docker-auto", "pod-1"]            # nothing else was touched, the leader stays
    assert status["done"] == ["docker-auto"] and world.max_not_ready == 1


def test_run_stops_when_the_leadership_moves():
    world = World(_prod())
    original_send = world.send

    def send_and_lose_leadership(target):
        original_send(target)
        world.is_leader = False                                    # e.g. the leader lost its lease

    world.send = send_and_lose_leadership
    status = world.run()
    assert status["status"] == "aborted" and "leadership moved" in status["message"]
    assert world.restarted == ["docker-auto"]


def test_plan_skips_old_cores_and_instances_that_are_not_alive():
    instances = [_inst("pod-1"), _inst("pod-2"), _inst("old-pod", api="1.13.0"),
                 _inst("pod-9", state="restarting", ready=False), _inst("run", ["api", "runner"])]
    order, skipped = plan(instances, "pod-1", include_runners=False)
    assert order == ["pod-2", "pod-1"]
    assert set(skipped) == {"old-pod", "pod-9", "run"} and "cannot be restarted one by one" in skipped["old-pod"]


def test_replacement_with_a_generated_id_is_recognised_by_its_hostname():
    """Without LYNDRIX_INSTANCE_ID the id is <hostname>-<4 hex>: new on every start."""
    world = World([_inst("web-a1b2", host="web"), _inst("lead-0001", host="lead")])

    def send(target):
        world.restarted.append(target.instance_id)
        del world.instances[target.instance_id]
        world.instances["web-c3d4"] = _inst("web-c3d4", host="web", incarnation="web-2")

    world.send = send
    status = world.run(leader="lead-0001")
    assert status["status"] == "done" and status["done"] == ["web-a1b2"]


# --- wiring in the cluster service ---------------------------------------------------

@pytest.fixture()
def published():
    seen: list = []

    def hook(topic, payload, forced):
        if topic in (RESTART_REQUEST_TOPIC, RESTART_INSTANCE_TOPIC):
            seen.append((topic, dict(payload), forced))

    previous = bus._cluster_publisher
    bus.set_cluster_publisher(hook)
    yield seen
    bus.set_cluster_publisher(previous)


def _service(iid="pod-1"):
    return ClusterService(instance_id=iid, roles=["api", "leader"], mode="cluster", driver="local",
                          subscribe=False, attach_bridge=False)


def test_a_non_leader_hands_the_request_to_the_leader(published):
    svc = _service()
    svc.leader_id = "pod-2"
    svc._instances = [_inst("pod-1"), _inst("pod-2"), _inst("docker-auto-runner", ["runner"])]

    async def run():                                               # the bus dispatches on a running loop
        answer = svc.request_restart("admin")
        svc._instances[1] = _inst("pod-2", api="1.13.0")          # the leader is still on an old core
        with pytest.raises(RestartRefused) as err:
            svc.request_restart("admin")
        return answer, str(err.value)

    answer, refusal = asyncio.run(run())
    assert answer["strategy"] == "sequential" and answer["order"] == ["pod-1", "pod-2"] and answer["leader"] == "pod-2"
    assert published == [(RESTART_REQUEST_TOPIC, {"requested_by": "admin", "include_runners": False}, True)]
    assert "finish the rolling update" in refusal


def test_only_the_addressed_incarnation_restarts(monkeypatch):
    svc = _service()
    calls: list = []
    monkeypatch.setattr(svc, "_restart_self", lambda: calls.append("SIGTERM"))

    async def run():
        await svc._on_restart_instance({"instance_id": "pod-2", "incarnation": None, "_origin": "pod-2"})
        await svc._on_restart_instance({"instance_id": "pod-1", "incarnation": "a-previous-process"})
        assert calls == []
        await svc._on_restart_instance({"instance_id": "pod-1", "incarnation": svc.incarnation, "requested_by": "a"})

    asyncio.run(run())
    assert calls == ["SIGTERM"]


def test_the_legacy_broadcast_of_an_old_instance_starts_a_sequential_run_on_the_leader(monkeypatch, published):
    svc = _service()
    started: list = []
    monkeypatch.setattr(svc, "_start_rolling_restart", lambda by, runners: started.append((by, runners)))

    async def run():
        legacy = {"requested_by": "admin", "order": ["pod-1", "old-pod"], "_origin": "old-pod"}
        await svc._on_restart_requested(legacy)                    # not the leader: ignore
        svc.is_leader = True
        await svc._on_restart_requested({"requested_by": "admin", "order": []})   # own/local event: ignore
        await svc._on_restart_requested(legacy)

    asyncio.run(run())
    assert started == [("admin", False)]                            # … and nobody exits "staggered by 5 s"
