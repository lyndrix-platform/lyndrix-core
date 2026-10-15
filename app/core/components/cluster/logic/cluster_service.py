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
  instance).
* SIGTERM → drain (cluster default 10 s), leases released, shutdown.
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

log = get_logger("Core:Cluster")

LEADER_LEASE = "core:leader"
CHANGED_TOPIC = "cluster:changed"
LEADER_TOPIC = "cluster:leader_changed"
RESTART_TOPIC = "system:restart_requested"
_RESTART_STAGGER_S = 5
_PREFLIGHT_REFRESH_S = 300
# Preflight failures that keep /api/ready at 503 for this instance.
_READINESS_CHECKS = frozenset({"coordinator", "storage_secret"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_instance_id() -> str:
    host = socket.gethostname().split(".")[0][:40] or "lyndrix"
    return f"{host}-{secrets.token_hex(2)}"


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

        self.coordinator: Coordinator = self._build_coordinator()
        self.leases = LeaseKeeper(self.coordinator, ttl=self.ttl, heartbeat_s=self.heartbeat_s)
        self.leases.on_change = self._on_lease_change
        self.bridge = ClusterBridge(self.coordinator, self.instance_id)
        self.drain = DrainController(seconds=self.drain_seconds, on_drain=self._on_drain)

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
        self._preflight_running = False

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
        if self.preflight_at is not None:
            # Boot finished before the coordinator came up (first-boot DDL race,
            # DB blip): the cached "coordinator failed" would keep /api/ready at
            # 503 — re-evaluate now that we are online.
            await self.run_preflight()

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
        await self._heartbeat_once()

    async def stop(self, reason: str = "shutdown") -> None:
        self.draining = True
        for task in (self._hb_task, self._start_task):
            if task is not None and not task.done():
                task.cancel()
        self._hb_task = self._start_task = None
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
        beats = 0
        while True:
            await asyncio.sleep(self.heartbeat_s)
            await self._heartbeat_once()
            beats += 1
            # Peers come and go; re-check the contracts every ~5 minutes so the
            # Cluster page and /api/ready never show a stale picture.
            if self.preflight_at is not None and beats * self.heartbeat_s >= _PREFLIGHT_REFRESH_S:
                beats = 0
                try:
                    await self.run_preflight()
                except Exception:
                    log.debug("CLUSTER: periodic preflight failed", exc_info=True)

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
        except Exception as exc:  # read-only or missing storage → the preflight reports it
            log.debug("CLUSTER: storage marker not written: %s", exc)

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

    async def _heartbeat_once(self) -> None:
        try:
            await self.coordinator.heartbeat(self._meta())
            if self.is_cluster:
                await asyncio.to_thread(self._write_storage_marker)
            await self.refresh_instances()
            await self._refresh_leader()
            self._hb_failures = 0
        except Exception as exc:
            self._hb_failures += 1
            if self._hb_failures in (1, 10, 100):
                log.warning("CLUSTER: heartbeat failed (%s×): %s", self._hb_failures, exc)

    def _meta(self) -> dict:
        ready, reasons = self.readiness()
        return {
            "ready": ready,
            "reasons": reasons,
            "draining": self.draining,
            "is_leader": self.is_leader,
            "pid": os.getpid(),
            "secret_fp": self.secret_fingerprint,
            "plugins_fp": self.plugins_fingerprint(),
            "ui_engines": settings.LYNDRIX_UI_ENGINE,
            "leases": self.leases.held,
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
            if self.preflight_at is not None and not self._preflight_running:
                bus.create_tracked_task(self._preflight_quietly(), name="cluster:preflight_refresh")
        return self._instances

    async def _preflight_quietly(self) -> None:
        self._preflight_running = True
        try:
            await self.run_preflight()
        except Exception:
            log.debug("CLUSTER: preflight after membership change failed", exc_info=True)
        finally:
            self._preflight_running = False

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
        """``/api/ready``: boot finished ∧ DB ∧ Vault ∧ not draining ∧ coordinator (cluster) ∧ no hard preflight failure."""
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
        if self.is_cluster and not self.started:
            reasons.append("coordinator")
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
            },
            "instances": [i.to_dict() for i in self._instances],
            "alive": len(alive),
            "leases": {"held": self.leases.held, "wanted": self.leases.wanted},
            "preflight": {**self.preflight_summary(), "checks": self.preflight_results},
            "settings": {
                "drain_seconds": self.drain_seconds,
                "trusted_proxies": settings.LYNDRIX_TRUSTED_PROXIES,
                "forwarded_for_seen": self.saw_forwarded_for,
            },
        }

    async def describe(self) -> dict:
        """Full picture for ``GET /api/cluster`` (fresh instance + lease lists)."""
        try:
            await self.refresh_instances()
        except Exception as exc:
            log.debug("CLUSTER: instance refresh failed: %s", exc)
        data = self.snapshot()
        try:
            data["lease_table"] = [l.to_dict() for l in await self.coordinator.leases()]
        except Exception as exc:
            data["lease_table"] = []
            data["coordinator"]["error"] = data["coordinator"].get("error") or str(exc)[:200]
        try:
            data["coordinator"]["server"] = await self.coordinator.server_info()
        except Exception as exc:
            data["coordinator"]["server"] = {"error": str(exc)[:200]}
        data["placements"] = self.placements()
        return data


cluster_service = ClusterService()
