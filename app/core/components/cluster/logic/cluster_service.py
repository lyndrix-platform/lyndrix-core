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
import os
import secrets
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from core.bus import bus
from core.logger import get_logger

from .bridge import ClusterBridge
from .coordinator import Coordinator, InstanceInfo
from .drain import DrainController
from .leases import RELEASED_TOPIC, LeaseKeeper
from .storage import StorageProbe

log = get_logger("Core:Cluster")

LEADER_LEASE = "core:leader"
CHANGED_TOPIC = "cluster:changed"
LEADER_TOPIC = "cluster:leader_changed"
RESTART_TOPIC = "system:restart_requested"
_RESTART_STAGGER_S = 5
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
    def __init__(self) -> None:
        self.mode: str = settings.LYNDRIX_CLUSTER_MODE
        self.driver: str = settings.LYNDRIX_COORDINATOR or "local"
        self.instance_id: str = (settings.LYNDRIX_INSTANCE_ID or _default_instance_id())[:100]
        self.roles: list[str] = list(settings.instance_roles)
        self.ttl = int(settings.LYNDRIX_LEASE_TTL_SECONDS)
        self.heartbeat_s = int(settings.LYNDRIX_LEASE_HEARTBEAT_SECONDS)
        self.poll_ms = int(settings.LYNDRIX_EVENT_POLL_MS)
        self.drain_seconds = int(settings.LYNDRIX_DRAIN_SECONDS or 0)
        self.started_at = _utcnow()

        # Injectable for tests (heartbeat staleness, lease deadlines, storage probe age).
        self._clock = time.monotonic
        self.coordinator: Coordinator = self._build_coordinator()
        self.leases = LeaseKeeper(self.coordinator, ttl=self.ttl, heartbeat_s=self.heartbeat_s,
                                  clock=lambda: self._clock())
        self.leases.on_change = self._on_lease_change
        self.bridge = ClusterBridge(self.coordinator, self.instance_id)
        self.drain = DrainController(seconds=self.drain_seconds, on_drain=self._on_drain)
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

        bus.subscribe("db:connected")(self.on_db_connected)
        bus.subscribe("system:boot_complete")(self.on_boot_complete)
        bus.subscribe(RESTART_TOPIC)(self._on_restart_requested)
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
            )
        from .drivers.local import LocalCoordinator

        return LocalCoordinator(instance_id=self.instance_id, ttl=self.ttl)

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
        try:
            await self.run_preflight()
        except Exception:
            log.error("CLUSTER: preflight failed to run", exc_info=True)
        if self._preflight_task is None or self._preflight_task.done():
            self._preflight_task = bus.create_tracked_task(self._preflight_loop(), name="cluster:preflight")
        await self._heartbeat_once()

    async def stop(self, reason: str = "shutdown") -> None:
        self.draining = True
        for task in (self._hb_task, self._start_task, self._preflight_task):
            if task is not None and not task.done():
                task.cancel()
        self._hb_task = self._start_task = self._preflight_task = None
        try:
            await self.leases.stop()
        except Exception:
            log.debug("CLUSTER: lease release on stop failed", exc_info=True)
        self.bridge.detach()
        try:
            await self.coordinator.stop()
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
        try:
            marker_dir = os.path.join(settings.STORAGE_DIR, self.STORAGE_MARKER_DIR)
            os.makedirs(marker_dir, exist_ok=True)
            path = os.path.join(marker_dir, self.instance_id)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_utcnow().isoformat())
            self.storage_last_ok_at, self.storage_last_error = _utcnow(), None
        except Exception as exc:  # read-only or missing storage → the preflight reports it
            self.storage_last_error = str(exc)[:200]
            log.debug("CLUSTER: storage marker not written: %s", exc)

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

    async def read_storage_markers(self, timeout: float = _IO_TIMEOUT_S) -> dict[str, float]:
        """:meth:`storage_markers` through the storage probe: bounded, and never
        a second thread on a mount that already holds one hostage. Raises
        :class:`StorageBusy` / ``asyncio.TimeoutError``."""
        return await self.storage.call(self.storage_markers, label="read markers", timeout=timeout)

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
        except Exception as exc:
            self._record_heartbeat_failure(str(exc)[:200] or exc.__class__.__name__, started)
        else:
            self._record_heartbeat_success(started)

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
            "pid": os.getpid(),
            "secret_fp": self.secret_fingerprint,
            "plugins_fp": self.plugins_fingerprint(),
            "ui_engines": settings.LYNDRIX_UI_ENGINE,
            "leases": self.leases.held,
            "storage_stuck_s": round(self.storage.stuck_for_s),
        }

    async def refresh_instances(self) -> list[InstanceInfo]:
        before = {i.instance_id for i in self._instances if i.alive}
        self._instances = await self.coordinator.instances()
        self._instances_at = time.monotonic()
        after = {i.instance_id for i in self._instances if i.alive}
        if before != after:
            if self._instances_at and before:  # not the very first fill
                log.info("CLUSTER: live instances changed %s → %s", sorted(before), sorted(after))
            self._emit_changed("instances")
            # Membership changed → contracts (role coverage, secret, versions)
            # may have changed with it; re-check instead of waiting 5 minutes.
            self.request_preflight()
        return self._instances

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

    def _emit_changed(self, reason: str) -> None:
        try:
            bus.emit(CHANGED_TOPIC, {"reason": reason, "instance_id": self.instance_id, "leader": self.leader_id})
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
            elif self.heartbeat_stale:
                reasons.append("heartbeat")
        # Only contract failures that make THIS instance unfit for traffic gate
        # readiness. A cluster-level problem (e.g. no live runner instance) is a
        # red check on the Cluster page, not a reason to take every instance out.
        reasons.extend(
            f"preflight:{c['id']}" for c in self.preflight_results
            if c.get("status") == "fail" and c.get("id") in _READINESS_CHECKS
        )
        return (not reasons, reasons)

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

    async def force_release(self, name: str) -> bool:
        if self.leases.holds(name):
            return await self.leases.release_now(name)
        return await self.coordinator.force_release(name)

    def request_restart(self, requested_by: str) -> dict:
        """Cluster mode: ask every instance to exit gracefully, staggered, leader last."""
        order = sorted(i.instance_id for i in self._instances if i.alive) or [self.instance_id]
        if self.leader_id in order:
            order.remove(self.leader_id)
            order.append(self.leader_id)
        payload = {"requested_by": requested_by, "order": order, "requested_at": _utcnow().isoformat()}
        bus.emit(RESTART_TOPIC, payload)  # bridged → every instance (incl. this one) handles it
        return payload

    async def _on_restart_requested(self, payload: Optional[dict] = None) -> None:
        if not self.is_cluster:
            return
        order = list((payload or {}).get("order") or [])
        position = order.index(self.instance_id) if self.instance_id in order else len(order)
        delay = position * _RESTART_STAGGER_S
        log.warning("CLUSTER: restart requested by %s — exiting in %ss (position %s/%s)",
                    (payload or {}).get("requested_by"), delay, position + 1, max(len(order), 1))
        await asyncio.sleep(delay)
        import signal

        os.kill(os.getpid(), signal.SIGTERM)  # → drain → uvicorn shutdown → restart policy / kubelet

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
                "bridge": {"attached": self.bridge.attached, "published": self.bridge.published, "relayed": self.bridge.relayed},
                "heartbeat": self.heartbeat_status(),
            },
            "instances": [i.to_dict() for i in self._instances],
            "alive": len(alive),
            "leases": {"held": self.leases.held, "wanted": self.leases.wanted},
            "storage": self.storage_status(),
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
