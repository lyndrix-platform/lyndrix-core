"""Cluster-wide plugin operations (core 1.8.0).

An install, upgrade or rollback the operator triggers on ONE instance is a
single *operation* the whole cluster works through:

- the leading instance (the one executing the API call) opens the operation
  with every live instance as a participant,
- every instance reports its own outcome once it loaded (or failed to load)
  the new files — the lead directly from ``_handle_plugin_install``, the
  followers after their reconcile install,
- whoever closes the last participant closes the operation and emits exactly
  one ``plugin:operation_finished`` event, which the Plugin Manager turns into
  exactly one notification ("state_monitoring 1.6.1 — 6/6 instances").

Progress is broadcast as ``plugin:operation_changed`` (SSE + cluster bridge)
for the Plugins page. Participants that never report are closed as
``skipped`` by the leader's timeout sweep. Without a database (boot, single
mode before db:connected) every call is a no-op and the classic per-event
notifications apply.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, TypeVar

from core.bus import bus
from core.components.database.logic.db_service import db_instance
from core.logger import get_logger

from .models import PluginOperation, PluginOperationInstance

log = get_logger("Core:PluginOps")

CHANGED_TOPIC = "plugin:operation_changed"
FINISHED_TOPIC = "plugin:operation_finished"
TERMINAL_STATES = ("done", "failed", "reverted", "skipped", "gone")   # gone = instance left the cluster
PROGRESS_STATES = ("installing",)                                     # alive and working — never times out
DEFAULT_TIMEOUT_S = 600          # no participant activity for this long → close
HARD_TIMEOUT_S = 3600            # … but never run longer than this
# A report attaches to the newest *running* operation of the plugin that is
# younger than this; an older running one is stale (closed by the sweep soon).
MATCH_WINDOW_S = 3600
_RETRY_CODES = ("1020", "1205", "1213")

T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _retry(fn: Callable[[], T], attempts: int = 4) -> T:
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt + 1 < attempts and any(c in str(getattr(exc, "orig", exc)) for c in _RETRY_CODES):
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


class OperationTracker:
    """DB-backed tracker; every method is blocking — call via ``asyncio.to_thread``."""

    # ------------------------------------------------------------------ infra

    @staticmethod
    def available() -> bool:
        return bool(db_instance.is_connected and db_instance.engine and db_instance.SessionLocal)

    @staticmethod
    def _session():
        session = db_instance.SessionLocal()
        try:
            bind = session.get_bind()
            if bind is not None and getattr(bind.dialect, "name", "") in ("mysql", "mariadb"):
                session.connection(execution_options={"isolation_level": "READ COMMITTED"})
        except Exception:
            pass
        return session

    @staticmethod
    def _cluster():
        from core.components.cluster.logic.cluster_service import cluster_service

        return cluster_service

    def instance_id(self) -> str:
        try:
            return str(self._cluster().instance_id)
        except Exception:
            return "local"

    def live_instances(self) -> list[str]:
        """Instance ids that must report (live ones; at least this instance)."""
        ids: list[str] = []
        try:
            svc = self._cluster()
            ids = sorted(i.instance_id for i in (getattr(svc, "_instances", None) or []) if getattr(i, "alive", False))
        except Exception:
            ids = []
        me = self.instance_id()
        if me not in ids:
            ids.append(me)
        return ids

    # ------------------------------------------------------------------ writes

    def start(self, *, plugin_id: Optional[str], repo_name: Optional[str], action: str,
              version: Optional[str], requested_by: Optional[str]) -> Optional[int]:
        """Open an operation with every live instance as participant. Returns its id."""
        if not self.available():
            return None
        participants = self.live_instances()
        now = _utcnow()

        def _tx() -> int:
            with self._session() as s:
                op = PluginOperation(
                    plugin_id=plugin_id, repo_name=repo_name, action=action, version=version,
                    requested_by=requested_by, requested_at=now, status="running",
                )
                s.add(op)
                s.flush()
                for iid in participants:
                    s.add(PluginOperationInstance(operation_id=op.id, instance_id=iid, status="pending", updated_at=now))
                s.commit()
                return int(op.id)

        try:
            op_id = _retry(_tx)
        except Exception as exc:
            log.warning(f"OPS: could not open operation for {plugin_id or repo_name}: {exc!s:.160}")
            return None
        log.info(f"OPS: #{op_id} {action} {plugin_id or repo_name} → {version or 'latest'} on {len(participants)} instance(s)")
        self._emit_changed(op_id, plugin_id, "running", 0, len(participants))
        return op_id

    def report(self, *, plugin_id: Optional[str], repo_name: Optional[str], status: str,
               message: Optional[str] = None, version: Optional[str] = None,
               instance_id: Optional[str] = None) -> Optional[int]:
        """Record this instance's outcome (or progress, ``installing``) for the
        newest running operation of the plugin; closes the operation when every
        participant reported a terminal state."""
        if not self.available() or status not in TERMINAL_STATES + PROGRESS_STATES:
            return None
        me = instance_id or self.instance_id()
        now = _utcnow()
        cutoff = now - timedelta(seconds=MATCH_WINDOW_S)
        live = set(self.live_instances())

        def _tx():
            with self._session() as s:
                q = s.query(PluginOperation).filter(
                    PluginOperation.status == "running", PluginOperation.requested_at >= cutoff,
                )
                ops = q.order_by(PluginOperation.id.desc()).all()
                op = next(
                    (o for o in ops
                     if (plugin_id and o.plugin_id == plugin_id) or (repo_name and o.repo_name == repo_name)),
                    None,
                )
                if op is None:
                    return None
                if plugin_id and not op.plugin_id:
                    op.plugin_id = plugin_id
                if version and (not op.version or op.version == "latest"):
                    op.version = version   # the resolved release replaces a "latest" request
                row = s.get(PluginOperationInstance, {"operation_id": op.id, "instance_id": me})
                if row is None:
                    row = PluginOperationInstance(operation_id=op.id, instance_id=me)
                    s.add(row)
                row.status = status
                row.message = (message or "")[:2000] or None
                row.updated_at = now
                s.flush()
                rows = s.query(PluginOperationInstance).filter(PluginOperationInstance.operation_id == op.id).all()
                self._mark_gone(rows, live, now)
                finished = self._maybe_close(op, rows, now)
                s.commit()
                return op.id, op.plugin_id, op.status, finished, self._counts(rows)

        try:
            result = _retry(_tx)
        except Exception as exc:
            log.warning(f"OPS: report for {plugin_id or repo_name} failed: {exc!s:.160}")
            return None
        if result is None:
            return None
        op_id, pid, op_status, finished, (done, total) = result
        log.info(f"OPS: #{op_id} {pid}: {me} → {status} ({done}/{total})")
        self._emit_changed(op_id, pid, op_status, done, total)
        if finished is not None:
            self._emit_finished(finished)
        return op_id

    def has_running(self, *, plugin_id: Optional[str] = None, repo_name: Optional[str] = None) -> bool:
        if not self.available() or not (plugin_id or repo_name):
            return False
        cutoff = _utcnow() - timedelta(seconds=MATCH_WINDOW_S)
        try:
            with self._session() as s:
                ops = s.query(PluginOperation).filter(
                    PluginOperation.status == "running", PluginOperation.requested_at >= cutoff,
                ).all()
                return any(
                    (plugin_id and o.plugin_id == plugin_id) or (repo_name and o.repo_name == repo_name)
                    for o in ops
                )
        except Exception:
            return False

    def sweep_timeouts(self, max_age_s: int = DEFAULT_TIMEOUT_S) -> int:
        """Close running operations older than ``max_age_s``: participants that
        never reported become ``skipped``. Leader-only by convention."""
        if not self.available():
            return 0
        now = _utcnow()
        finished_payloads: list[dict] = []
        live = set(self.live_instances())

        def _tx() -> int:
            closed = 0
            with self._session() as s:
                running = s.query(PluginOperation).filter(PluginOperation.status == "running").all()
                for op in running:
                    rows = s.query(PluginOperationInstance).filter(PluginOperationInstance.operation_id == op.id).all()
                    # Instances that left (a rollout replaced the pods) never report:
                    # mark them gone, which may close the operation on the live ones.
                    if self._mark_gone(rows, live, now):
                        payload = self._maybe_close(op, rows, now)
                        if payload is not None:
                            finished_payloads.append(payload)
                            closed += 1
                            continue
                    # Timeout is activity based: a plugin with heavy requirements
                    # keeps six instances busy for minutes (pip into vendor/) —
                    # as long as someone reports progress the operation lives on.
                    last = max([op.requested_at] + [r.updated_at for r in rows if r.updated_at])
                    idle_for = (now - last).total_seconds()
                    age = (now - op.requested_at).total_seconds()
                    if idle_for < max_age_s and age < HARD_TIMEOUT_S:
                        continue
                    for row in rows:
                        if row.status not in TERMINAL_STATES:
                            row.status = "skipped"
                            row.message = f"no report within {int(idle_for)}s"
                            row.updated_at = now
                    payload = self._maybe_close(op, rows, now)
                    if payload is not None:
                        finished_payloads.append(payload)
                        closed += 1
                s.commit()
            return closed

        try:
            closed = _retry(_tx)
        except Exception as exc:
            log.warning(f"OPS: timeout sweep failed: {exc!s:.160}")
            return 0
        for payload in finished_payloads:
            log.warning(f"OPS: #{payload['operation_id']} {payload['plugin_id']} closed by timeout as {payload['status']}")
            self._emit_changed(payload["operation_id"], payload["plugin_id"], payload["status"], payload["done"], payload["total"])
            self._emit_finished(payload)
        return closed

    # ------------------------------------------------------------------ reads

    def list(self, *, active_only: bool = False, limit: int = 20) -> list[dict]:
        if not self.available():
            return []
        with self._session() as s:
            q = s.query(PluginOperation)
            if active_only:
                q = q.filter(PluginOperation.status == "running")
            ops = q.order_by(PluginOperation.id.desc()).limit(max(1, min(int(limit), 200))).all()
            if not ops:
                return []
            ids = [o.id for o in ops]
            rows = s.query(PluginOperationInstance).filter(PluginOperationInstance.operation_id.in_(ids)).all()
            by_op: dict[int, list[PluginOperationInstance]] = {}
            for r in rows:
                by_op.setdefault(r.operation_id, []).append(r)
            return [self._to_dict(o, by_op.get(o.id, [])) for o in ops]

    def get(self, op_id: int) -> Optional[dict]:
        if not self.available():
            return None
        with self._session() as s:
            op = s.get(PluginOperation, op_id)
            if op is None:
                return None
            rows = s.query(PluginOperationInstance).filter(PluginOperationInstance.operation_id == op.id).all()
            return self._to_dict(op, rows)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _counts(rows) -> tuple[int, int]:
        """(done, total) — instances that left the cluster do not count."""
        active = [r for r in rows if r.status != "gone"]
        return sum(1 for r in active if r.status == "done"), len(active)

    @staticmethod
    def _mark_gone(rows, live: set, now: datetime) -> bool:
        """Pending participants that are not alive any more → ``gone``. Returns True when any changed."""
        changed = False
        for r in rows:
            if r.status == "pending" and live and r.instance_id not in live:
                r.status = "gone"
                r.message = "instance left the cluster"
                r.updated_at = now
                changed = True
        return changed

    def _maybe_close(self, op: PluginOperation, rows, now: datetime) -> Optional[dict]:
        """Close ``op`` when every participant is terminal; returns the finished payload."""
        if any(r.status not in TERMINAL_STATES for r in rows):
            return None
        done = sorted(r.instance_id for r in rows if r.status == "done")
        failed = [
            {"instance_id": r.instance_id, "status": r.status, "message": r.message}
            for r in sorted(rows, key=lambda r: r.instance_id) if r.status in ("failed", "reverted")
        ]
        pending = sorted(r.instance_id for r in rows if r.status == "skipped")
        total = sum(1 for r in rows if r.status != "gone")
        if failed or pending:
            op.status = "partial" if done else "failed"
        else:
            op.status = "done"
        op.finished_at = now
        op.summary = json.dumps({"done": done, "failed": failed, "pending": pending,
                                 "gone": sorted(r.instance_id for r in rows if r.status == "gone")})
        return {
            "operation_id": op.id, "plugin_id": op.plugin_id, "repo_name": op.repo_name,
            "action": op.action, "version": op.version, "requested_by": op.requested_by,
            "status": op.status, "done": len(done), "total": total, "failed": failed, "pending": pending,
        }

    @staticmethod
    def _to_dict(op: PluginOperation, rows) -> dict:
        rows = sorted(rows, key=lambda r: r.instance_id)
        return {
            "id": op.id, "plugin_id": op.plugin_id, "repo_name": op.repo_name, "action": op.action,
            "version": op.version, "requested_by": op.requested_by,
            "requested_at": op.requested_at.isoformat() if op.requested_at else None,
            "finished_at": op.finished_at.isoformat() if op.finished_at else None,
            "status": op.status,
            "done": sum(1 for r in rows if r.status == "done"), "total": len(rows),
            "instances": [
                {"instance_id": r.instance_id, "status": r.status, "message": r.message,
                 "updated_at": r.updated_at.isoformat() if r.updated_at else None}
                for r in rows
            ],
        }

    @staticmethod
    def _emit_changed(op_id: int, plugin_id: Optional[str], status: str, done: int, total: int) -> None:
        try:
            bus.emit(CHANGED_TOPIC, {"operation_id": op_id, "plugin_id": plugin_id, "status": status,
                                     "done": done, "total": total})
        except Exception:
            log.debug("OPS: operation_changed emit failed", exc_info=True)

    @staticmethod
    def _emit_finished(payload: dict) -> None:
        try:
            bus.emit(FINISHED_TOPIC, payload)
        except Exception:
            log.debug("OPS: operation_finished emit failed", exc_info=True)


operation_tracker = OperationTracker()
