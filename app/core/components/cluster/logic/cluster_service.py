"""ClusterService — the one object the rest of the core talks to about "us vs. the others".

Owns the coordinator (driver from ``LYNDRIX_COORDINATOR``), the LeaseKeeper, the
bus bridge and the drain controller; knows this instance's id and roles, who
the leader is, which peers are alive, whether *this* instance is ready for
traffic (``/api/ready``) and what the preflight found.

Lifecycle (both modes, one code path):

* ``db:connected`` (first time) → coordinator starts, bridge attaches, the
  leader lease ``core:leader`` is wanted when the instance has the ``leader``
  role, heartbeats begin. In ``single`` mode the ``local`` driver grants the
  lease immediately — ``ctx.is_leader`` is ``True`` before any plugin loads.
* ``system:boot_complete`` → preflight runs once (hard failures keep
  ``/api/ready`` at 503 so a load balancer never routes to a misconfigured
  instance) and then periodically in its own task.
* SIGTERM → drain (cluster default 10 s), leases released, shutdown.

Three loops, deliberately independent so one cannot starve another: the
heartbeat (registry row + peer view, every ``heartbeat_s``), the LeaseKeeper
(lease renewals) and the preflight (contract checks, every five minutes or
when asked). The heartbeat never waits for the preflight or for the storage
mount, and readiness is derived from the live coordinator/heartbeat state
rather than from a cached check result.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from config import settings
from core.bus import bus
from core.logger import get_logger

from .bridge import ClusterBridge
from .coordinator import Coordinator, InstanceIdConflict, InstanceInfo
from .drain import DrainController
from .leases import RELEASED_TOPIC, LeaseKeeper
from .origin import clean_context
from .restart import (
    RESTART_INSTANCE_TOPIC,
    RESTART_REQUEST_TOPIC,
    RestartRefused,
    RollingRestart,
    plan as plan_restart,
    supports_sequential_restart,
)
from .storage import StorageProbe

log = get_logger("Core:Cluster")

LEADER_LEASE = "core:leader"
CHANGED_TOPIC = "cluster:changed"
LEADER_TOPIC = "cluster:leader_changed"
# LOCAL event (never bridged): every live instance emits it once when it
# declares a peer dead — {instance_id, roles, last_heartbeat, incarnation,
# graceful}. Not for a graceful restart within the grace; again if the peer
# comes back and dies later.
INSTANCE_LOST_TOPIC = "cluster:instance_lost"
# Bridged: "holder, please hand lease <name> over" (admin release of a lease
# that a live peer holds — the holder stops its work before the lease moves).
LEASE_HANDOVER_TOPIC = "cluster:lease_handover_requested"
_STATE_RANK = {"alive": 4, "draining": 3, "restarting": 2, "unknown": 1, "dead": 0}
# Legacy broadcast of core ≤ 1.11 ("everybody restart, staggered by 5 s"). Never
# emitted any more; when an old instance sends it during a rollout, the leader
# answers with a sequential restart instead (see restart.py).
RESTART_TOPIC = "system:restart_requested"
_PREFLIGHT_REFRESH_S = 300
# A check that could not decide (timeout, transient error, peer too young) asks
# for a re-run after this many heartbeats instead of the full refresh interval;
# after _PREFLIGHT_MAX_QUICK_RECHECKS in a row the normal interval applies again
# (a permanent warning must not turn into a check every 20 seconds).
_PREFLIGHT_RECHECK_BEATS = 2
_PREFLIGHT_MAX_QUICK_RECHECKS = 5
# Upper bound for one coordinator round trip made on behalf of an API request
# or a preflight check (the driver's own socket timeout is much longer).
_IO_TIMEOUT_S = 8.0
# Preflight failures that keep /api/ready at 503 for this instance. Coordinator
# health is NOT in here: readiness reads it live (``started`` + heartbeat age),
# a cached check result would pin /api/ready at 503 long after a blip is over.
_READINESS_CHECKS = frozenset({"storage_secret"})
_MARKER_CLEANUP_INTERVAL_S = 3600
_MARKER_MAX_AGE_S = 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_instance_id() -> str:
    host = socket.gethostname().split(".")[0][:40] or "lyndrix"
    return f"{host}-{secrets.token_hex(2)}"


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"coordinator did not answer within {_IO_TIMEOUT_S:.0f}s"
    return str(exc)[:200] or exc.__class__.__name__


class ClusterService:
    def __init__(self, *, instance_id: Optional[str] = None, roles: Optional[list[str]] = None,
                 mode: Optional[str] = None, driver: Optional[str] = None, ttl: Optional[float] = None,
                 heartbeat_s: Optional[float] = None, poll_ms: Optional[int] = None,
                 drain_seconds: Optional[int] = None, restart_grace_s: Optional[float] = None,
                 coordinator: Optional[Coordinator] = None, clock: Optional[Callable[[], float]] = None,
                 subscribe: bool = True, attach_bridge: bool = True) -> None:
        """The process-wide instance (``cluster_service`` below) takes everything
        from the settings. The keyword arguments exist for tests that run
        several instances in one process against one database: pass a
        coordinator per instance, ``subscribe=False`` (the bus handlers are
        called by the test) and ``attach_bridge=False`` (the global bus has one
        publisher hook)."""
        self.mode: str = mode or settings.LYNDRIX_CLUSTER_MODE
        self.driver: str = driver or settings.LYNDRIX_COORDINATOR or "local"
        self.instance_id: str = (instance_id or settings.LYNDRIX_INSTANCE_ID or _default_instance_id())[:100]
        self.roles: list[str] = list(roles if roles is not None else settings.instance_roles)
        self.ttl = ttl if ttl is not None else int(settings.LYNDRIX_LEASE_TTL_SECONDS)
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else int(settings.LYNDRIX_LEASE_HEARTBEAT_SECONDS)
        self.poll_ms = int(poll_ms if poll_ms is not None else settings.LYNDRIX_EVENT_POLL_MS)
        self.drain_seconds = int(drain_seconds if drain_seconds is not None else settings.LYNDRIX_DRAIN_SECONDS or 0)
        self.restart_grace_s = float(
            restart_grace_s if restart_grace_s is not None else settings.LYNDRIX_RESTART_GRACE_SECONDS)
        self.started_at = _utcnow()
        # Boot nonce: peers tell a restart under a fixed instance id from a
        # continuing instance by it (ctx.incarnation, ctx.instance_state()).
        self.incarnation = secrets.token_hex(8)
        self._attach_bridge = attach_bridge

        # Injectable for tests (heartbeat staleness, lease deadlines, storage probe age).
        self._clock = clock or time.monotonic
        self.coordinator: Coordinator = coordinator if coordinator is not None else self._build_coordinator()
        self.incarnation = getattr(self.coordinator, "incarnation", None) or self.incarnation
        self.leases = LeaseKeeper(self.coordinator, ttl=self.ttl, heartbeat_s=self.heartbeat_s,
                                  clock=lambda: self._clock())
        self.leases.on_change = self._on_lease_change
        self.bridge = ClusterBridge(self.coordinator, self.instance_id)
        self.drain = DrainController(seconds=self.drain_seconds, on_drain=self._on_drain,
                                     on_drain_start=self.begin_drain)
        # Work assignment / load balancing for plugins (core API >= 1.8): which
        # instances run a named workload, and which share of its keys is ours.
        from .assignment import WorkAssigner

        self.assigner = WorkAssigner(self)

        self.started = False
        self.start_error: Optional[str] = None
        self.draining = False
        self.is_leader = False
        self.leader_id: Optional[str] = None
        self.saw_forwarded_for = False
        self.preflight_results: list[dict] = []
        self.preflight_at: Optional[str] = None
        self._instances: list[InstanceInfo] = []
        self._instances_at = 0.0
        # clock() of the last SUCCESSFUL refresh of the view (0 = never).
        self._instances_ok_at = 0.0
        # What this instance remembers about its peers between refreshes:
        # id → {"state", "incarnation", "roles", "last_heartbeat", "gone_at",
        # "graceful", "announced"}. Feeds instance_state() for rows that are no
        # longer in the table and makes cluster:instance_lost fire exactly once.
        self._peers: dict[str, dict] = {}
        # Set while another process uses this instance id (heartbeat refused).
        self.id_conflict: Optional[str] = None
        # clock() when boot completed — start of the wait for plugin convergence.
        self._boot_complete_at: Optional[float] = None
        self.plugin_wait_s = float(settings.LYNDRIX_READY_PLUGIN_WAIT_SECONDS)
        self._drain_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._restart_task: Optional[asyncio.Task] = None
        # Progress of the rolling restart this instance leads/led (GET /api/cluster → "restart").
        self.restart_status: Optional[dict] = None
        self._hb_task: Optional[asyncio.Task] = None
        self._start_task: Optional[asyncio.Task] = None
        self._hb_failures = 0
        self._hb_last_ok_mono = 0.0
        # Monotonic START time of the last successful beat — the moment the row
        # was written, i.e. what the peers measure this instance's age from.
        self._hb_last_ok_started = 0.0
        self.last_heartbeat_ok_at: Optional[datetime] = None
        self.last_heartbeat_error: Optional[str] = None
        self.last_heartbeat_duration_s: Optional[float] = None
        self._preflight_running = False
        self._preflight_task: Optional[asyncio.Task] = None
        self._preflight_wake: Optional[asyncio.Event] = None
        self._preflight_recheck = False
        self._preflight_quick_rechecks = 0
        # All STORAGE_DIR I/O of the cluster layer: one job at a time, own thread.
        self.storage = StorageProbe(clock=lambda: self._clock())
        self.storage_last_error: Optional[str] = None
        self.storage_last_ok_at: Optional[datetime] = None
        self._markers_cleaned_at = 0.0

        if subscribe:
            bus.subscribe("db:connected")(self.on_db_connected)
            bus.subscribe("system:boot_complete")(self.on_boot_complete)
            bus.subscribe(RESTART_TOPIC)(self._on_restart_requested)
            bus.subscribe(RESTART_REQUEST_TOPIC)(self._on_restart_requested)
            bus.subscribe(RESTART_INSTANCE_TOPIC)(self._on_restart_instance)
            bus.subscribe(LEASE_HANDOVER_TOPIC)(self._on_handover_requested)
            bus.subscribe(RELEASED_TOPIC)(self._on_peer_released_lease)

    def _on_peer_released_lease(self, payload: Optional[dict] = None) -> None:
        """A peer gave a lease up (bridged event) → try to acquire wanted leases now."""
        if (payload or {}).get("_origin") and self.started:
            self.leases.kick()

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------

    def _build_coordinator(self) -> Coordinator:
        if self.driver == "db":
            from .drivers.db import DbCoordinator

            return DbCoordinator(
                instance_id=self.instance_id, ttl=self.ttl, heartbeat_s=self.heartbeat_s,
                poll_ms=self.poll_ms, should_prune=lambda: self.is_leader,
                incarnation=self.incarnation, restart_grace_s=self.restart_grace_s, roles=self.roles,
            )
        from .drivers.local import LocalCoordinator

        return LocalCoordinator(instance_id=self.instance_id, ttl=self.ttl, incarnation=self.incarnation)

    @property
    def is_cluster(self) -> bool:
        return self.mode == "cluster"

    @property
    def leader_capable(self) -> bool:
        return "leader" in self.roles

    @property
    def secret_fingerprint(self) -> str:
        """Non-reversible fingerprint of STORAGE_SECRET for the equality preflight."""
        raw = (settings.STORAGE_SECRET or "").encode("utf-8")
        return hashlib.sha256(b"lyndrix-secret-fp:" + raw).hexdigest()[:16]

    def plugins_fingerprint(self) -> str:
        """Hash of the loaded plugin set (id@version) — artifact drift detection."""
        try:
            from core.components.plugins.logic.manager import module_manager

            items = sorted(
                f"{mid}@{entry['manifest'].version}"
                for mid, entry in module_manager.registry.items()
                if entry.get("manifest") and entry["manifest"].type == "PLUGIN"
            )
        except Exception:
            items = []
        return hashlib.sha256("|".join(items).encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def on_startup(self) -> None:
        """Called from the FastAPI startup hook (event loop running)."""
        log.info(
            "CLUSTER: mode=%s driver=%s instance=%s roles=%s lease ttl=%ss drain=%ss",
            self.mode, self.driver, self.instance_id, ",".join(self.roles), self.ttl, self.drain_seconds,
        )
        self.drain.install()

    async def on_db_connected(self, payload: Optional[dict] = None) -> None:
        if self.started or (self._start_task is not None and not self._start_task.done()):
            return  # DB reconnect — the coordinator keeps its own retry/poll loop
        try:
            # First attempt inline: in single mode the leader lease is held before
            # the first module calls ctx.is_leader.
            await self._start_once()
        except Exception as exc:
            self._record_start_failure(exc, attempt=1)
            self._start_task = bus.create_tracked_task(self._retry_start(), name="cluster:start_retry")

    async def _start_once(self) -> None:
        await self.coordinator.start()
        if self._attach_bridge:
            self.bridge.attach()
        await self.leases.start()
        if self.leader_capable:
            self.leases.want(LEADER_LEASE, acquired=self._leader_acquired, lost=self._leader_lost)
        await self.leases.tick()
        await self._heartbeat_once()
        if self._hb_task is None or self._hb_task.done():
            self._hb_task = bus.create_tracked_task(self._heartbeat_loop(), name="cluster:heartbeat")
        self.started = True
        self.start_error = None
        # Central log: ship this instance's log lines to the shared table (both modes).
        try:
            from .log_store import log_store

            log_store.start(self.instance_id, lambda: self.is_leader)
        except Exception:
            log.debug("CLUSTER: log store not started", exc_info=True)
        log.info("CLUSTER: coordinator '%s' online (leader: %s)", self.driver, self.leader_id or "pending")
        self._emit_changed("started")
        # Boot finished before the coordinator came up (first-boot DDL race, DB
        # blip): the Cluster page still shows "coordinator not started" from
        # that run — re-evaluate now that we are online.
        self.request_preflight()

    def _record_start_failure(self, exc: Exception, *, attempt: int) -> None:
        self.start_error = str(exc)[:300]
        log.error("CLUSTER: coordinator start failed (attempt %s): %s", attempt, exc, exc_info=attempt == 1)
        self._emit_changed("start_failed")

    async def _retry_start(self) -> None:
        """Keep trying — a first-boot DDL race or a DB blip must not leave an
        instance without coordination forever (it would stay unready)."""
        delay, attempt = 3, 1
        while not self.started:
            await asyncio.sleep(delay)
            attempt += 1
            try:
                await self._start_once()
            except Exception as exc:
                self._record_start_failure(exc, attempt=attempt)
                delay = min(delay * 2, 30)

    async def on_boot_complete(self, payload: Optional[dict] = None) -> None:
        if self._boot_complete_at is None:
            self._boot_complete_at = self._clock()
        try:
            await self.run_preflight()
        except Exception:
            log.error("CLUSTER: preflight failed to run", exc_info=True)
        if self._preflight_task is None or self._preflight_task.done():
            self._preflight_task = bus.create_tracked_task(self._preflight_loop(), name="cluster:preflight")
        await self._heartbeat_once()

    def begin_drain(self) -> None:
        """SIGTERM arrived (called synchronously by the DrainController).

        Everything that tells the outside world "do not count on me any more"
        happens NOW, not after the drain sleep: ``/api/ready`` answers 503,
        one heartbeat publishes ``draining`` (peers stop assigning shards and
        know this is a planned stop), and the leases are handed over — ``lost``
        callbacks first, then the release, so a peer runs the leader and worker
        duties while this instance still finishes its in-flight requests.
        """
        if self.draining:
            return
        self.draining = True
        try:
            self._drain_task = bus.create_tracked_task(self._drain_start(), name="cluster:drain_start")
        except RuntimeError:
            self._drain_task = None  # no running loop (sync test code) — stop() does the hand-over

    async def _drain_start(self) -> None:
        await self._heartbeat_once()
        try:
            await self.leases.stop()
        except Exception:
            log.warning("CLUSTER: lease hand-over at drain start failed", exc_info=True)
        self._emit_changed("draining")

    async def stop(self, reason: str = "shutdown") -> None:
        """Leave the cluster: hand the leases over (if the drain start has not
        done so), detach the bridge, leave a tombstone. Idempotent — uvicorn's
        shutdown hook calls it again after a drain."""
        if self._stopped:
            return
        self._stopped = True
        self.draining = True
        for task in (self._hb_task, self._start_task, self._preflight_task):
            if task is not None and not task.done():
                task.cancel()
        self._hb_task = self._start_task = self._preflight_task = None
        if self._drain_task is not None and not self._drain_task.done():
            # The hand-over started at SIGTERM is still waiting for lost callbacks.
            await asyncio.wait([self._drain_task], timeout=LeaseKeeper.LOST_CALLBACK_TIMEOUT_S + 1)
        try:
            await self.leases.stop()
        except Exception:
            log.debug("CLUSTER: lease release on stop failed", exc_info=True)
        if self.bridge.attached:
            await self.bridge.flush()   # let the release broadcasts out before the bridge goes
        self.bridge.detach()
        try:
            await asyncio.wait_for(self.coordinator.stop(), timeout=_IO_TIMEOUT_S)
        except Exception:
            log.debug("CLUSTER: coordinator stop failed", exc_info=True)
        self.started = False
        log.info("CLUSTER: stopped (%s)", reason)

    def _on_drain(self):
        self.draining = True
        return self.stop("drain")

    # ------------------------------------------------------------------
    # heartbeat / leader tracking
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Beat at a fixed rate and do nothing else.

        The preflight used to run inline here: a slow check (hung storage
        mount, black-holed DB) stopped every heartbeat for as long as it took
        and the peers declared this instance dead. It lives in its own task
        now. Fixed rate (not "sleep a full interval after each beat") keeps the
        row fresh when a beat is slow: the next one starts as soon as its slot
        is due.
        """
        delay = float(self.heartbeat_s)
        while True:
            await asyncio.sleep(delay)
            started = self._clock()
            await self._heartbeat_once()
            delay = self._next_beat_delay(started)

    def _next_beat_delay(self, started: float) -> float:
        """Seconds to sleep before the next beat, given when the last one started."""
        return max(0.0, started + self.heartbeat_s - self._clock())

    # Marker files that let the preflight tell whether STORAGE_DIR is one shared
    # mount (every instance sees the others' markers) or instance-local disks.
    STORAGE_MARKER_DIR = ".cluster"

    def _write_storage_marker(self) -> None:
        """Write this instance's marker: a small JSON file naming the process
        (incarnation) that wrote it. A peer that reads the same incarnation the
        registry row carries has proof that both look at one directory — no
        directory listing and no file times involved (both lie on NFS: listing
        and attribute caches, server clock vs. reader clock)."""
        try:
            marker_dir = os.path.join(settings.STORAGE_DIR, self.STORAGE_MARKER_DIR)
            os.makedirs(marker_dir, exist_ok=True)
            path = os.path.join(marker_dir, self.instance_id)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"instance_id": self.instance_id, "incarnation": self.incarnation,
                           "written_at": _utcnow().isoformat()}, fh)
            self.storage_last_ok_at, self.storage_last_error = _utcnow(), None
            if self.is_leader and time.monotonic() - self._markers_cleaned_at > _MARKER_CLEANUP_INTERVAL_S:
                self._markers_cleaned_at = time.monotonic()
                self._remove_stale_markers(marker_dir)
        except Exception as exc:  # read-only or missing storage → the preflight reports it
            self.storage_last_error = str(exc)[:200]
            log.debug("CLUSTER: storage marker not written: %s", exc)

    def _remove_stale_markers(self, marker_dir: str) -> int:
        """Leader housekeeping (runs on the storage thread): every live
        instance rewrites its marker each heartbeat, so a marker untouched for
        a day belongs to an instance that is long gone (one file per pod name
        would otherwise pile up forever)."""
        known = {i.instance_id for i in self._instances} | {self.instance_id}
        removed = 0
        cutoff = time.time() - _MARKER_MAX_AGE_S
        for name in os.listdir(marker_dir):
            path = os.path.join(marker_dir, name)
            try:
                if name not in known and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
        if removed:
            log.info("CLUSTER: removed %s storage marker(s) of instances gone for more than a day", removed)
        return removed

    def _read_peer_markers(self, instance_ids: list[str]) -> dict[str, Optional[str]]:
        """instance id → what its marker in OUR directory says: the incarnation
        that wrote it, ``"legacy"`` for a marker of core ≤ 1.11 (a timestamp,
        no JSON) or ``None`` when no readable marker exists. Opened by name:
        open() revalidates on NFS, a directory listing may be minutes old."""
        out: dict[str, Optional[str]] = {}
        marker_dir = os.path.join(settings.STORAGE_DIR, self.STORAGE_MARKER_DIR)
        for iid in instance_ids:
            try:
                with open(os.path.join(marker_dir, iid), encoding="utf-8") as fh:
                    raw = fh.read(4096)
            except OSError:
                out[iid] = None
                continue
            try:
                out[iid] = str(json.loads(raw).get("incarnation") or "legacy")
            except Exception:
                out[iid] = "legacy"
        return out

    async def read_peer_markers(self, instance_ids: list[str], timeout: float = _IO_TIMEOUT_S) -> dict:
        """:meth:`_read_peer_markers` through the storage probe (bounded, one
        storage job at a time). Raises :class:`StorageBusy` / ``asyncio.TimeoutError``."""
        return await self.storage.call(self._read_peer_markers, list(instance_ids),
                                       label="read markers", timeout=timeout)

    def _drop_storage_marker(self) -> None:
        """Refresh this instance's marker without ever waiting for the mount.

        Skipped while the previous storage job has not returned: a hung mount
        costs one blocked thread, not one per beat.
        """
        if self.storage.submit(self._write_storage_marker, label="marker") is None:
            stuck = self.storage.stuck_for_s
            if stuck >= self.ttl and int(stuck) % max(60, self.ttl) < self.heartbeat_s:
                log.warning(
                    "CLUSTER: storage job '%s' has not returned for %.0fs — STORAGE_DIR (%s) is not responding; "
                    "marker writes are skipped until it does",
                    self.storage.stuck_label, stuck, settings.STORAGE_DIR,
                )

    def storage_status(self) -> dict:
        """Storage probe telemetry for ``GET /api/cluster`` and the heartbeat meta."""
        return {
            **self.storage.status(),
            "dir": settings.STORAGE_DIR,
            "last_ok_at": self.storage_last_ok_at.isoformat() if self.storage_last_ok_at else None,
            "last_error": self.storage_last_error,
        }

    def storage_markers(self) -> dict[str, float]:
        """instance_id → marker age in seconds, for every marker visible from here."""
        out: dict[str, float] = {}
        marker_dir = os.path.join(settings.STORAGE_DIR, self.STORAGE_MARKER_DIR)
        try:
            now = time.time()
            for name in os.listdir(marker_dir):
                try:
                    out[name] = max(0.0, now - os.path.getmtime(os.path.join(marker_dir, name)))
                except OSError:
                    continue
        except OSError:
            pass
        return out

    @property
    def heartbeat_timeout_s(self) -> float:
        """A beat that has not finished within the lease TTL is a missed beat —
        the peers already count this instance as dead by then."""
        return float(max(self.ttl, self.heartbeat_s))

    async def _heartbeat_once(self) -> None:
        """One heartbeat: register, drop the storage marker, refresh peers/leader.

        Bounded by :attr:`heartbeat_timeout_s`: a hung database socket (peer
        vanished, kernel still retransmitting) used to park the heartbeat task —
        and with it every later beat — until the TCP timeout (~16 min). Now the
        beat is abandoned after the TTL (the worker thread finishes on its own
        once pymysql's ``read_timeout`` fires), the failure is logged with its
        duration, and the next beat runs on a fresh pooled connection.
        """
        started = self._clock()
        try:
            await asyncio.wait_for(self._heartbeat_body(), timeout=self.heartbeat_timeout_s)
        except asyncio.TimeoutError:
            self._record_heartbeat_failure(f"timed out after {self.heartbeat_timeout_s:.0f}s", started)
            # The pooled connection (and probably its siblings) has a dead peer:
            # let the next beat open a fresh one instead of pre-pinging the
            # remaining dead sockets, each for a full socket timeout.
            self._refresh_coordinator_connections("heartbeat timed out")
        except InstanceIdConflict as exc:
            self._record_heartbeat_failure(str(exc)[:300], started)
            await self._stand_back(str(exc))
        except Exception as exc:
            self._record_heartbeat_failure(str(exc)[:200] or exc.__class__.__name__, started)
        else:
            self._record_heartbeat_success(started)
            if self.id_conflict is not None:
                await self._step_forward()

    async def _stand_back(self, reason: str) -> None:
        """Another running process owns this instance id now: leave the load
        balancer (readiness ``instance_id_conflict``) and stop acting as lease
        holder — the rows are that process's to renew."""
        if self.id_conflict is None:
            log.critical("CLUSTER: %s", reason)
            await self.leases.suspend()
            self._emit_changed("id_conflict")
        self.id_conflict = reason

    async def _step_forward(self) -> None:
        log.warning("CLUSTER: instance id conflict is over — '%s' is this process again", self.instance_id)
        self.id_conflict = None
        if not self.draining:
            await self.leases.start()
        self._emit_changed("id_conflict")

    async def _heartbeat_body(self) -> None:
        await self.coordinator.heartbeat(self._meta())
        if self.is_cluster:
            self._drop_storage_marker()   # fire and forget — never waits for the mount
        await self.refresh_instances()
        await self._refresh_leader()

    def _refresh_coordinator_connections(self, reason: str) -> None:
        refresh = getattr(self.coordinator, "refresh_connections", None)
        if callable(refresh):
            try:
                refresh(reason)
            except Exception:
                log.debug("CLUSTER: connection refresh failed", exc_info=True)

    def _record_heartbeat_success(self, started: float) -> None:
        now = self._clock()
        elapsed = now - started
        self.last_heartbeat_duration_s = elapsed
        if self._hb_failures:
            gap = now - self._hb_last_ok_mono if self._hb_last_ok_mono else 0.0
            log.warning(
                "CLUSTER: heartbeat recovered after %s failure(s), %.0fs without a successful beat (lease ttl %ss)",
                self._hb_failures, gap, self.ttl,
            )
        elif elapsed > self.heartbeat_s:
            # Slow but successful: the DB answered later than the interval —
            # the early warning before beats start timing out.
            log.warning("CLUSTER: heartbeat took %.1fs (interval %ss) — database latency", elapsed, self.heartbeat_s)
        self._hb_failures = 0
        self._hb_last_ok_mono = now
        self._hb_last_ok_started = started
        self.last_heartbeat_ok_at = _utcnow()
        self.last_heartbeat_error = None
        if self._preflight_recheck:
            # A check could not decide while the coordinator was struggling —
            # do not leave its warning on the Cluster page for five minutes.
            self.request_preflight()

    def _record_heartbeat_failure(self, error: str, started: float) -> None:
        elapsed = self._clock() - started
        self._hb_failures += 1
        self.last_heartbeat_error = error
        self.last_heartbeat_duration_s = elapsed
        since_ok = self._clock() - self._hb_last_ok_mono if self._hb_last_ok_mono else None
        # Every one of the first three failures, then about once a minute —
        # enough to see the outage start, last and end in the log without a
        # line per beat.
        every = max(1, 60 // max(1, self.heartbeat_s))
        if self._hb_failures <= 3 or self._hb_failures % every == 0:
            log.warning(
                "CLUSTER: heartbeat failed (%s×, %.1fs, %s since the last successful beat; peers see this instance "
                "dead after %ss): %s",
                self._hb_failures, elapsed, f"{since_ok:.0f}s" if since_ok is not None else "never succeeded",
                self.ttl, error,
            )

    @property
    def heartbeat_stale(self) -> bool:
        """No successful heartbeat within the lease TTL: the peers already count
        this instance as dead (they measure from the moment the row was
        written, hence the START of the last good beat). ``True`` as well while
        no beat has succeeded at all."""
        if not self._hb_last_ok_started:
            return True
        return self._clock() - self._hb_last_ok_started > self.ttl

    def heartbeat_status(self) -> dict:
        """Heartbeat telemetry for ``GET /api/cluster`` (Cluster page) and the logs."""
        return {
            "interval_s": self.heartbeat_s,
            "timeout_s": self.heartbeat_timeout_s,
            "stale": self.heartbeat_stale if self.is_cluster else False,
            "failures": self._hb_failures,
            "last_ok_at": self.last_heartbeat_ok_at.isoformat() if self.last_heartbeat_ok_at else None,
            "last_duration_s": round(self.last_heartbeat_duration_s, 2) if self.last_heartbeat_duration_s is not None else None,
            "last_error": self.last_heartbeat_error,
        }

    def _meta(self) -> dict:
        _, reasons = self.readiness()
        # This meta is written BY a heartbeat: when it lands, the heartbeat
        # works. A stale one shows in the row's age instead — publishing
        # "not ready: heartbeat" would stick for a whole interval after recovery.
        reasons = [r for r in reasons if r != "heartbeat"]
        return {
            "ready": not reasons,
            "reasons": reasons,
            "draining": self.draining,
            "is_leader": self.is_leader,
            "incarnation": self.incarnation,
            "pid": os.getpid(),
            "secret_fp": self.secret_fingerprint,
            "plugins_fp": self.plugins_fingerprint(),
            "ui_engines": settings.LYNDRIX_UI_ENGINE,
            "leases": self.leases.held,
            "storage_stuck_s": round(self.storage.stuck_for_s),
        }

    async def refresh_instances(self) -> list[InstanceInfo]:
        before = {i.instance_id for i in self._instances if i.alive}
        fresh_before = self.view_ready
        self._instances = await self.coordinator.instances()
        self._instances_at = time.monotonic()
        self._instances_ok_at = self._clock()
        after = {i.instance_id for i in self._instances if i.alive}
        delta = self._track_peers(fresh_before)
        if before != after or delta:
            if self._instances_at and before and before != after:  # not the very first fill
                log.info("CLUSTER: live instances changed %s → %s", sorted(before), sorted(after))
            self._emit_changed("instances", joined=sorted(after - before), left=sorted(before - after), **delta)
            # Membership changed → contracts (role coverage, secret, versions)
            # may have changed with it; re-check instead of waiting 5 minutes.
            self.request_preflight()
        return self._instances

    @property
    def view_ready(self) -> bool:
        """Is this instance's picture of the others trustworthy right now?

        Single mode: always. Cluster mode: the coordinator started and the
        instance list was read successfully within the last TTL (+ one
        heartbeat). While it is not, answers about peers are ``unknown`` —
        never "dead": a fresh pod whose first refresh failed, or an instance
        cut off from the database, must not conclude that its peers are gone.
        """
        if not self.is_cluster:
            return True
        if not self.started or not self._instances_ok_at:
            return False
        return self._clock() - self._instances_ok_at <= self.ttl + self.heartbeat_s

    def _track_peers(self, view_was_fresh: bool) -> dict:
        """Fold the fresh instance list into what we remember about the peers;
        emits ``cluster:instance_lost`` for every peer that is dead now and has
        not been announced in this life of it. Returns the state changes worth
        a ``cluster:changed`` (``restarting`` / ``lost`` id lists)."""
        now = self._clock()
        rows = {i.instance_id: i for i in self._instances if not i.is_self}
        restarting: list[str] = []
        lost: list[str] = []
        for iid, info in rows.items():
            peer = self._peers.setdefault(iid, {"announced": None})
            previous = peer.get("state")
            peer.update(state=info.state, incarnation=info.incarnation, roles=list(info.roles),
                        last_heartbeat=info.heartbeat_at, gone_at=None,
                        graceful=info.state == "restarting" or bool(info.meta.get("draining")))
            if info.state in ("alive", "draining"):
                peer["announced"] = None          # alive again → a later death is news again
            elif info.state == "restarting" and previous != "restarting":
                restarting.append(iid)
            elif info.state == "dead" and self._announce_lost(iid, peer):
                lost.append(iid)
        for iid in [i for i in self._peers if i not in rows]:
            peer = self._peers[iid]
            if peer.get("gone_at") is None:
                peer["gone_at"] = now
                if peer.get("state") in ("alive", "draining") and view_was_fresh:
                    # Alive a moment ago, row gone now: only a graceful stop of
                    # a core ≤ 1.11 instance deletes its row at once (a crash
                    # leaves it, the leader prunes silent rows after minutes).
                    peer.update(state="restarting", graceful=True)
                    restarting.append(iid)
            if peer.get("state") == "restarting" and now - peer["gone_at"] > self.restart_grace_s:
                peer["state"] = "dead"
            if peer.get("state") != "restarting":
                if peer.get("state") != "dead" or self._announce_lost(iid, peer):
                    lost.append(iid)
                self._peers.pop(iid, None)     # nothing left to remember
        delta = {}
        if restarting:
            delta["restarting"] = sorted(restarting)
        if lost:
            delta["lost"] = sorted(set(lost))
        return delta

    def _announce_lost(self, iid: str, peer: dict) -> bool:
        marker = peer.get("incarnation") or "-"
        if peer.get("announced") == marker:
            return False
        peer["announced"] = marker
        beat = peer.get("last_heartbeat")
        log.warning("CLUSTER: instance %s is dead (last heartbeat %s%s)", iid, beat or "unknown",
                    ", after a graceful stop" if peer.get("graceful") else "")
        try:
            bus.emit(INSTANCE_LOST_TOPIC, {
                "instance_id": iid, "roles": list(peer.get("roles") or []),
                "last_heartbeat": beat.isoformat() if isinstance(beat, datetime) else None,
                "incarnation": peer.get("incarnation"), "graceful": bool(peer.get("graceful")),
                "observed_by": self.instance_id,
            })
        except Exception:
            log.debug("CLUSTER: instance_lost emit failed", exc_info=True)
        return True

    def instance_state(self, instance_id: str) -> dict:
        """What this instance knows about ``instance_id`` (``ctx.instance_state``):
        ``{"instance_id", "state", "known", "roles", "incarnation",
        "last_heartbeat", "age_s"}`` with state ``alive`` | ``draining`` |
        ``restarting`` | ``dead`` | ``unknown``.

        ``unknown`` means *this* instance cannot tell right now (its view of
        the registry is missing or stale) — ask again later, never treat it as
        dead. An id that is in neither the table nor memory is ``dead`` with
        ``known: False`` (rows of dead instances are pruned after a few minutes).
        """
        out = {"instance_id": instance_id, "state": "unknown", "known": False, "roles": [],
               "incarnation": None, "last_heartbeat": None, "age_s": None}
        if instance_id == self.instance_id:
            out.update(state="draining" if self.draining else "alive", known=True, roles=list(self.roles),
                       incarnation=self.incarnation,
                       last_heartbeat=self.last_heartbeat_ok_at.replace(tzinfo=None).isoformat()
                       if self.last_heartbeat_ok_at else None)
            return out
        if not self.view_ready:
            return out
        info = next((i for i in self._instances if i.instance_id == instance_id), None)
        if info is not None:
            out.update(state=info.state, known=True, roles=list(info.roles), incarnation=info.incarnation,
                       last_heartbeat=info.heartbeat_at.isoformat() if isinstance(info.heartbeat_at, datetime) else None,
                       age_s=round(info.age_s, 1))
            return out
        peer = self._peers.get(instance_id)
        if peer is not None:
            beat = peer.get("last_heartbeat")
            out.update(state=peer.get("state") or "dead", known=True, roles=list(peer.get("roles") or []),
                       incarnation=peer.get("incarnation"),
                       last_heartbeat=beat.isoformat() if isinstance(beat, datetime) else None)
            return out
        out["state"] = "dead"
        return out

    def role_state(self, role: str) -> dict:
        """Best state among the instances carrying ``role`` (``ctx.role_state``):
        ``{"role", "state", "instances": {id: state}}``. ``restarting`` = the
        role is momentarily gone but expected back (graceful restart within the
        grace); ``unknown`` = this instance's view is not usable yet."""
        states: dict[str, str] = {}
        if role in self.roles:
            states[self.instance_id] = "draining" if self.draining else "alive"
        if self.is_cluster and self.view_ready:
            for info in self._instances:
                if not info.is_self and role in info.roles:
                    states[info.instance_id] = info.state
            for iid, peer in self._peers.items():
                if iid not in states and role in (peer.get("roles") or []) and peer.get("state") == "restarting":
                    states[iid] = "restarting"
        if states:
            best = max(states.values(), key=lambda state: _STATE_RANK.get(state, 0))
        else:
            best = "dead" if self.view_ready else "unknown"
        if best == "dead" and not self.view_ready:
            best = "unknown"
        return {"role": role, "state": best, "instances": states}

    def request_preflight(self) -> None:
        """Ask for a preflight run now (membership change, coordinator back,
        a check that wanted a second look). Never runs it inline: the caller is
        the heartbeat or the start path and must not wait for the checks."""
        if self.preflight_at is None:
            return  # boot has not finished — the first run happens on system:boot_complete
        if self._preflight_task is not None and not self._preflight_task.done():
            if self._preflight_wake is not None:
                self._preflight_wake.set()
            return
        if not self._preflight_running:
            try:
                bus.create_tracked_task(self._preflight_quietly(), name="cluster:preflight_refresh")
            except RuntimeError:
                pass  # no running loop (sync test code) — the next periodic run covers it

    async def _preflight_quietly(self) -> None:
        if self._preflight_running:
            return
        self._preflight_running = True
        try:
            await self.run_preflight()
        except Exception:
            log.debug("CLUSTER: preflight run failed", exc_info=True)
        finally:
            self._preflight_running = False

    def _preflight_delay(self) -> float:
        """Seconds until the next periodic run: the refresh interval, or a
        couple of heartbeats when the last run left something undecided."""
        if self._preflight_recheck and self._preflight_quick_rechecks < _PREFLIGHT_MAX_QUICK_RECHECKS:
            return float(_PREFLIGHT_RECHECK_BEATS * self.heartbeat_s)
        return float(_PREFLIGHT_REFRESH_S)

    async def _preflight_loop(self) -> None:
        """Peers come and go; re-check the contracts every ~5 minutes (sooner
        on request) so the Cluster page never shows a stale picture. Its own
        task: nothing in here can delay a heartbeat or a lease renewal."""
        self._preflight_wake = asyncio.Event()
        while True:
            delay = self._preflight_delay()
            quick = delay < _PREFLIGHT_REFRESH_S
            try:
                await asyncio.wait_for(self._preflight_wake.wait(), timeout=delay)
                quick = False  # asked for explicitly — not one of the automatic re-checks
            except asyncio.TimeoutError:
                pass
            self._preflight_wake.clear()
            if quick:
                self._preflight_quick_rechecks += 1
            await self._preflight_quietly()

    async def _refresh_leader(self) -> None:
        info = await self.coordinator.holder(LEADER_LEASE)
        leader = info.holder if info is not None and not info.expired else None
        if leader != self.leader_id:
            previous, self.leader_id = self.leader_id, leader
            log.info("CLUSTER: leader is now %s (was %s)", leader or "nobody", previous or "nobody")
            self._emit_changed("leader")

    def _leader_acquired(self) -> None:
        self.is_leader = True
        self.leader_id = self.instance_id
        bus.emit(LEADER_TOPIC, {"leader": self.instance_id, "is_self": True, "instance_id": self.instance_id})
        self._emit_changed("leader")

    def _leader_lost(self) -> None:
        self.is_leader = False
        if self.leader_id == self.instance_id:
            self.leader_id = None
        bus.emit(LEADER_TOPIC, {"leader": None, "is_self": False, "instance_id": self.instance_id})
        self._emit_changed("leader")

    def _on_lease_change(self, name: str, held: bool) -> None:
        if name != LEADER_LEASE:
            self._emit_changed("lease")

    def _emit_changed(self, reason: str, **details) -> None:
        try:
            bus.emit(CHANGED_TOPIC, {"reason": reason, "instance_id": self.instance_id, "leader": self.leader_id,
                                     **details})
        except Exception:
            log.debug("CLUSTER: change emit failed", exc_info=True)

    # ------------------------------------------------------------------
    # readiness / preflight
    # ------------------------------------------------------------------

    def readiness(self) -> tuple[bool, list[str]]:
        """``/api/ready``: boot finished ∧ DB ∧ Vault ∧ not draining ∧ coordinator
        started and heart-beating (cluster) ∧ no hard preflight failure.

        Everything about the coordinator is read live. In cluster mode an
        instance without a successful heartbeat for a full lease TTL is dead to
        its peers (no leases, no shards, "no runner online") — it must not keep
        answering 200 to the load balancer just because the process is up.
        """
        reasons: list[str] = []
        if self.draining:
            reasons.append("draining")
        try:
            from core.services import boot_service, db_instance, vault_instance

            phase = getattr(boot_service, "phase", None)
            if getattr(boot_service, "is_booting", True) or str(getattr(phase, "value", phase)) != "ready":
                reasons.append(f"boot:{getattr(phase, 'value', phase)}")
            if not getattr(db_instance, "is_connected", False):
                reasons.append("database")
            if not getattr(vault_instance, "is_connected", False):
                reasons.append("vault")
        except Exception:  # pragma: no cover - import-order edge case during tests
            reasons.append("core")
        if self.is_cluster:
            if not self.started:
                reasons.append("coordinator")
            elif self.id_conflict is not None:
                reasons.append("instance_id_conflict")
            elif self.heartbeat_stale:
                reasons.append("heartbeat")
            if self.plugins_converging and not any(r.startswith("boot:") for r in reasons):
                reasons.append("plugins:converging")
        # Only contract failures that make THIS instance unfit for traffic gate
        # readiness. A cluster-level problem (e.g. no live runner instance) is a
        # red check on the Cluster page, not a reason to take every instance out.
        reasons.extend(
            f"preflight:{c['id']}" for c in self.preflight_results
            if c.get("status") == "fail" and c.get("id") in _READINESS_CHECKS
        )
        return (not reasons, reasons)

    @staticmethod
    def _plugins_converged() -> bool:
        """Did the module manager finish its boot-time convergence (saved
        plugins activated, missing ones installed)? Managers without the flag
        (older cores, tests) count as converged."""
        try:
            from core.components.plugins.logic.manager import module_manager

            return bool(getattr(module_manager, "boot_converged", True))
        except Exception:
            return True

    def plugin_wait_elapsed_s(self) -> float:
        """Seconds since boot completed (0 until then)."""
        return 0.0 if self._boot_complete_at is None else max(0.0, self._clock() - self._boot_complete_at)

    @property
    def plugins_converging(self) -> bool:
        """Cluster mode: plugins are still being installed/activated after boot
        and the wait (``LYNDRIX_READY_PLUGIN_WAIT_SECONDS``) is not over."""
        if not self.is_cluster or self.plugin_wait_s <= 0 or self._plugins_converged():
            return False
        return self.plugin_wait_elapsed_s() < self.plugin_wait_s

    async def run_preflight(self) -> list[dict]:
        from .preflight import run_checks

        checks = await run_checks(self)
        self.preflight_results = [c.to_dict() for c in checks]
        self.preflight_at = _utcnow().isoformat()
        # Undecided checks (timeout, transient error) get a second look soon.
        self._preflight_recheck = any(c.recheck for c in checks)
        if not self._preflight_recheck:
            self._preflight_quick_rechecks = 0
        for check in checks:
            if check.status == "fail":
                log.error("PREFLIGHT [%s] FAIL: %s%s", check.id, check.message, f" — {check.hint}" if check.hint else "")
            elif check.status == "warn":
                log.warning("PREFLIGHT [%s] WARN: %s%s", check.id, check.message, f" — {check.hint}" if check.hint else "")
        summary = self.preflight_summary()
        log.info("PREFLIGHT: %s ok, %s warn, %s fail, %s skipped",
                 summary["ok"], summary["warn"], summary["fail"], summary["skip"])
        self._emit_changed("preflight")
        return self.preflight_results

    def preflight_summary(self) -> dict:
        counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
        for check in self.preflight_results:
            counts[check.get("status", "skip")] = counts.get(check.get("status", "skip"), 0) + 1
        return {**counts, "checked_at": self.preflight_at}

    # ------------------------------------------------------------------
    # kv facade (OIDC state, SSO codes, rate limiter)
    # ------------------------------------------------------------------

    async def kv_set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        await self.coordinator.kv_set(key, value, ttl)

    async def kv_get(self, key: str) -> Any:
        return await self.coordinator.kv_get(key)

    async def kv_delete(self, key: str) -> bool:
        return await self.coordinator.kv_delete(key)

    async def kv_incr(self, key: str, ttl: Optional[int] = None) -> int:
        return await self.coordinator.kv_incr(key, ttl)

    # ------------------------------------------------------------------
    # admin actions
    # ------------------------------------------------------------------

    async def force_release(self, name: str, *, force: bool = False) -> bool:
        """Admin action "release this lease".

        Held here: handed over in order (``lost`` callbacks, release). Held by
        a live peer: that peer is asked to do the same — freeing its row behind
        its back left it working as holder until its next renewal while another
        instance already started. ``force=True`` (or a holder that is dead,
        unknown or on a core that does not know the request) frees the row
        directly.
        """
        if self.leases.holds(name):
            return await self.leases.release_now(name)
        if not force:
            info = await asyncio.wait_for(self.coordinator.holder(name), timeout=_IO_TIMEOUT_S)
            holder = next((i for i in self._instances if info is not None and i.instance_id == info.holder), None)
            if (info is not None and not info.expired and holder is not None and not holder.is_self
                    and holder.state in ("alive", "draining") and supports_sequential_restart(holder)):
                bus.emit(LEASE_HANDOVER_TOPIC, {"name": name, "holder": holder.instance_id}, cluster=True)
                return True
        return await self.coordinator.force_release(name)

    async def _on_handover_requested(self, payload: Optional[dict] = None) -> None:
        payload = payload or {}
        if payload.get("_origin") and payload.get("holder") == self.instance_id:
            name = str(payload.get("name") or "")
            if self.leases.holds(name):
                log.warning("CLUSTER: lease '%s' released on request of %s", name, payload.get("_origin"))
                await self.leases.release_now(name)

    def request_restart(self, requested_by: str, *, include_runners: bool = False) -> dict:
        """Cluster mode: restart the instances ONE AT A TIME, leader last.

        The leader drives the run (restart.py); when this instance is not the
        leader the request is handed to it over the bridge. Raises
        :class:`RestartRefused` when no leader can take it.
        """
        leader = next((i for i in self._instances if i.instance_id == self.leader_id), None)
        if not self.is_leader:
            if leader is None or leader.state != "alive":
                raise RestartRefused("no leader is known right now — try again in a moment")
            if not supports_sequential_restart(leader):
                raise RestartRefused(
                    f"the leader ({leader.instance_id}) runs core {leader.core_version}, which cannot coordinate a "
                    "sequential restart — finish the rolling update first")
        if self._restart_task is not None and not self._restart_task.done():
            raise RestartRefused("a rolling restart is already running")
        order, skipped = plan_restart(self._instances, self.leader_id or self.instance_id,
                                      include_runners=include_runners)
        payload = {"requested_by": requested_by, "include_runners": bool(include_runners),
                   "requested_at": _utcnow().isoformat(), "leader": self.leader_id,
                   "order": order, "skipped": skipped, "strategy": "sequential"}
        if self.is_leader:
            self._start_rolling_restart(requested_by, bool(include_runners))
        else:
            bus.emit(RESTART_REQUEST_TOPIC, {"requested_by": requested_by, "include_runners": bool(include_runners)},
                     cluster=True)
        return payload

    def _start_rolling_restart(self, requested_by: str, include_runners: bool) -> None:
        if self._restart_task is not None and not self._restart_task.done():
            log.warning("CLUSTER: rolling restart already running — request of %s ignored", requested_by)
            return

        async def _view() -> list[InstanceInfo]:
            return await asyncio.wait_for(self.refresh_instances(), timeout=_IO_TIMEOUT_S)

        run = RollingRestart(
            leader_id=self.instance_id, instances=_view, send=self._send_restart, restart_self=self._restart_self,
            is_leader=lambda: self.is_leader, requested_by=requested_by, include_runners=include_runners,
            instance_timeout_s=float(settings.LYNDRIX_RESTART_INSTANCE_TIMEOUT_SECONDS), clock=lambda: self._clock(),
            on_change=self._on_restart_progress,
        )
        self.restart_status = dict(run.status)
        # Clean context: a request forwarded by a peer starts this run inside
        # the relayed event's handler, and the bridge drops everything emitted
        # from there — the leader's "restart now" commands would never leave
        # this instance and every step would time out.
        self._restart_task = bus.create_tracked_task(run.run(), name="cluster:rolling_restart",
                                                     context=clean_context())

    def _on_restart_progress(self, status: dict) -> None:
        self.restart_status = status
        self._emit_changed("restart", restart=status)

    def _send_restart(self, target: InstanceInfo) -> None:
        bus.emit(RESTART_INSTANCE_TOPIC, {"instance_id": target.instance_id, "incarnation": target.incarnation,
                                          "requested_by": (self.restart_status or {}).get("requested_by")},
                 cluster=True)

    def _restart_self(self) -> None:
        import signal

        os.kill(os.getpid(), signal.SIGTERM)  # → drain → uvicorn shutdown → restart policy / kubelet

    async def _on_restart_requested(self, payload: Optional[dict] = None) -> None:
        """A peer asks for a rolling restart (``cluster:restart_requested``), or
        an instance of core ≤ 1.11 broadcast its "everybody restart" event.
        Only the leader acts, and only by restarting one instance at a time."""
        payload = payload or {}
        if not self.is_cluster or not payload.get("_origin") or not self.is_leader:
            return
        self._start_rolling_restart(str(payload.get("requested_by") or payload.get("_origin")),
                                    bool(payload.get("include_runners")))

    async def _on_restart_instance(self, payload: Optional[dict] = None) -> None:
        """The leader's "restart now" for one instance — this one?"""
        payload = payload or {}
        if not self.is_cluster or payload.get("instance_id") != self.instance_id:
            return
        if payload.get("incarnation") not in (None, self.incarnation):
            return  # meant for the process that ran under this id before us
        log.warning("CLUSTER: restart requested by %s (rolling restart) — draining and exiting",
                    payload.get("requested_by"))
        await asyncio.sleep(0.5)
        self._restart_self()

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    def placements(self) -> list[dict]:
        try:
            from core.components.plugins.logic.manager import module_manager
        except Exception:
            return []
        out = []
        for mid, entry in module_manager.registry.items():
            manifest = entry.get("manifest")
            if not manifest or manifest.type != "PLUGIN":
                continue
            placement = getattr(manifest, "placement", "any") or "any"
            role = placement.split(":", 1)[1] if placement.startswith("pinned:") else None
            lease = f"plugin:{mid}:singleton" if placement == "singleton" else None
            out.append({
                "plugin_id": mid,
                "name": manifest.name,
                "placement": placement,
                "role": role,
                "status": entry.get("status"),
                "lease": lease,
                "held_here": bool(lease and self.leases.holds(lease)),
            })
        return out

    def snapshot(self) -> dict:
        ready, reasons = self.readiness()
        alive = [i for i in self._instances if i.alive]
        return {
            "mode": self.mode,
            "driver": self.driver,
            "instance": {
                "id": self.instance_id,
                "hostname": socket.gethostname(),
                "roles": self.roles,
                "is_leader": self.is_leader,
                "ready": ready,
                "reasons": reasons,
                "draining": self.draining,
                "incarnation": self.incarnation,
                "id_conflict": self.id_conflict,
                "started_at": self.started_at.isoformat(),
                "uptime_s": round((_utcnow() - self.started_at).total_seconds()),
            },
            "leader": {"id": self.leader_id, "is_self": self.leader_id == self.instance_id if self.leader_id else False},
            "coordinator": {
                "started": self.started,
                "error": self.start_error,
                "ttl_s": self.ttl,
                "heartbeat_s": self.heartbeat_s,
                "poll_ms": self.poll_ms,
                "bridge": self.bridge.status(),
                "heartbeat": self.heartbeat_status(),
            },
            "instances": [i.to_dict() for i in self._instances],
            "alive": len(alive),
            "states": {state: sum(1 for i in self._instances if i.state == state)
                       for state in ("alive", "draining", "restarting", "dead")},
            "view_ready": self.view_ready,
            "leases": {"held": self.leases.held, "wanted": self.leases.wanted},
            "storage": self.storage_status(),
            "restart": self.restart_status,
            "work": self.assigner.report(),
            "preflight": {**self.preflight_summary(), "checks": self.preflight_results},
            "settings": {
                "drain_seconds": self.drain_seconds,
                "trusted_proxies": settings.LYNDRIX_TRUSTED_PROXIES,
                "forwarded_for_seen": self.saw_forwarded_for,
            },
        }

    async def describe(self) -> dict:
        """Full picture for ``GET /api/cluster`` (fresh instance + lease lists).

        Every coordinator round trip is bounded: with a black-holed database
        the page answers from the cached view (and says so) instead of hanging
        for three socket timeouts.
        """
        try:
            await asyncio.wait_for(self.refresh_instances(), timeout=_IO_TIMEOUT_S)
        except Exception as exc:
            log.debug("CLUSTER: instance refresh failed: %s", exc)
        data = self.snapshot()
        try:
            leases = await asyncio.wait_for(self.coordinator.leases(), timeout=_IO_TIMEOUT_S)
            data["lease_table"] = [lease.to_dict() for lease in leases]
        except Exception as exc:
            data["lease_table"] = []
            data["coordinator"]["error"] = data["coordinator"].get("error") or _describe_error(exc)
        try:
            data["coordinator"]["server"] = await asyncio.wait_for(
                self.coordinator.server_info(), timeout=_IO_TIMEOUT_S)
        except Exception as exc:
            data["coordinator"]["server"] = {"error": _describe_error(exc)}
        data["placements"] = self.placements()
        return data


cluster_service = ClusterService()
