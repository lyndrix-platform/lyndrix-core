"""Rolling restart of a cluster — one instance at a time, driven by the leader.

``POST /api/system/restart`` used to broadcast one event on which every
instance slept ``position × 5 s`` and exited. An instance needs 30–60 s to be
ready again, so from about the fifth second on nothing was serving: a "rolling"
restart that took the whole cluster down.

Now the leader walks through the instances: it tells ONE instance to restart
(``cluster:restart_instance``), waits until that instance is back — a new
incarnation that reports ready — and only then moves on. Itself comes last
(its drain hands the leader lease over). If an instance does not come back
within the timeout the run is aborted: better some instances on the old
process than half the cluster gone. Instances with the ``runner`` role are
left alone unless asked for (a restart orphans their running jobs), and so are
instances of an older core that do not know the topic.

The run lives in the leader's memory; when the leader changes mid-run it
simply stops (at most one instance was restarting).
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from packaging.version import InvalidVersion, Version

from core.logger import get_logger

from .coordinator import InstanceInfo

log = get_logger("Core:Cluster:Restart")

# Any instance → the leader: "run a rolling restart" (bridged, cluster=True).
RESTART_REQUEST_TOPIC = "cluster:restart_requested"
# The leader → one instance: "restart now" (bridged, cluster=True).
RESTART_INSTANCE_TOPIC = "cluster:restart_instance"
# First plugin API version whose core understands the two topics above.
MIN_API_VERSION = "1.14.0"


class RestartRefused(RuntimeError):
    """A rolling restart cannot be started right now (reason in the message)."""


def supports_sequential_restart(instance: InstanceInfo) -> bool:
    try:
        return Version(str(instance.api_version or "0")) >= Version(MIN_API_VERSION)
    except InvalidVersion:
        return False


def plan(instances: list[InstanceInfo], leader_id: str, *, include_runners: bool) -> tuple[list[str], dict[str, str]]:
    """(order, skipped): instance ids in restart order — the leader last — and
    why the others are left alone."""
    order: list[str] = []
    skipped: dict[str, str] = {}
    for inst in sorted(instances, key=lambda i: i.instance_id):
        if inst.state != "alive":
            skipped[inst.instance_id] = f"is {inst.state}"
        elif "runner" in inst.roles and not include_runners:
            skipped[inst.instance_id] = "has the runner role (restart it on request: include_runners)"
        elif not supports_sequential_restart(inst):
            skipped[inst.instance_id] = f"runs core {inst.core_version or '?'} which cannot be restarted one by one"
        else:
            order.append(inst.instance_id)
    if leader_id in order:
        order.remove(leader_id)
        order.append(leader_id)
    return order, skipped


class RollingRestart:
    def __init__(
        self,
        *,
        leader_id: str,
        instances: Callable[[], Awaitable[list[InstanceInfo]]],
        send: Callable[[InstanceInfo], None],
        restart_self: Callable[[], None],
        is_leader: Callable[[], bool],
        requested_by: str,
        include_runners: bool = False,
        instance_timeout_s: float = 300.0,
        poll_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_change: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._leader_id = leader_id
        self._instances = instances
        self._send = send
        self._restart_self = restart_self
        self._is_leader = is_leader
        self._include_runners = include_runners
        self._timeout = float(instance_timeout_s)
        self._poll_s = float(poll_s)
        self._clock = clock
        self._sleep = sleep
        self._on_change = on_change
        self.status: dict = {
            "status": "running", "requested_by": requested_by, "include_runners": include_runners,
            "leader": leader_id, "order": [], "done": [], "skipped": {}, "current": None, "message": None,
        }

    def _update(self, **fields) -> None:
        self.status.update(fields)
        if self._on_change is not None:
            try:
                self._on_change(dict(self.status))
            except Exception:
                log.debug("RESTART: status hook failed", exc_info=True)

    async def run(self) -> dict:
        order, skipped = plan(await self._instances(), self._leader_id, include_runners=self._include_runners)
        self._update(order=list(order), skipped=skipped)
        log.warning("RESTART: rolling restart requested by %s — order %s, skipped %s",
                    self.status["requested_by"], order, skipped or "none")
        for instance_id in order:
            if not self._is_leader():
                return self._abort("leadership moved to another instance")
            if instance_id == self._leader_id:
                break  # after the loop
            self._update(current=instance_id)
            problem = await self._restart_and_wait(instance_id)
            if problem:
                return self._abort(problem)
            self.status["done"].append(instance_id)
        if self._leader_id in order:
            # Last: this instance. Its drain hands the leader lease over.
            self._update(current=self._leader_id, status="done",
                         message="all other instances restarted — the leader restarts itself now")
            log.warning("RESTART: every other instance is back — restarting the leader (%s)", self._leader_id)
            self._restart_self()
        else:
            self._update(current=None, status="done", message="finished (the leader was not part of the run)")
        return dict(self.status)

    def _abort(self, reason: str) -> dict:
        log.error("RESTART: rolling restart aborted — %s (restarted so far: %s)", reason, self.status["done"])
        self._update(status="aborted", message=reason)
        return dict(self.status)

    async def _restart_and_wait(self, instance_id: str) -> Optional[str]:
        """Tell one instance to restart and wait for its replacement. Returns
        ``None`` on success, else the reason to abort."""
        instances = await self._instances()
        target = next((i for i in instances if i.instance_id == instance_id), None)
        if target is None or target.state != "alive":
            return None  # went away by itself meanwhile — nothing to restart
        # The replacement is recognised by a NEW incarnation under the same
        # instance id, or (generated ids change on restart) on the same host.
        known = {i.incarnation for i in instances
                 if i.incarnation and (i.instance_id == instance_id or i.hostname == target.hostname)}
        log.warning("RESTART: restarting %s (%s)", instance_id, target.hostname or "?")
        self._send(target)
        deadline = self._clock() + self._timeout
        while self._clock() < deadline:
            await self._sleep(self._poll_s)
            if not self._is_leader():
                return "leadership moved to another instance"
            try:
                instances = await self._instances()
            except Exception as exc:
                log.debug("RESTART: instance list unavailable (%s) — keep waiting", exc)
                continue
            for inst in instances:
                same = inst.instance_id == instance_id or (target.hostname and inst.hostname == target.hostname)
                if (same and inst.incarnation and inst.incarnation not in known
                        and inst.state == "alive" and inst.meta.get("ready") is True):
                    log.info("RESTART: %s is back and ready (as %s)", instance_id, inst.instance_id)
                    return None
        return f"{instance_id} did not come back ready within {self._timeout:.0f}s"
