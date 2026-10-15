"""Central log store (docs/ha/building-blocks/logs.md).

Every instance keeps its in-memory ring buffer (``core.logger.log_ring``) for the
local fast path. In addition a :class:`ClusterLogHandler` on the root logger
buffers each record, and :class:`LogStore` ships the buffer to the shared
``cluster_logs`` table every couple of seconds. ``GET /api/logs?instance=<id|all>``
then serves the log of any instance — or all of them merged — from whichever
instance the load balancer picked, and the lines survive a pod restart.

The leader prunes the table: rows older than ``LYNDRIX_LOG_DB_RETENTION_HOURS``
and anything beyond ``LYNDRIX_LOG_DB_MAX_PER_INSTANCE`` per instance. Shipping is
best effort: a disconnected database drops lines (counted in ``dropped``), it
never blocks logging or the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError

from core.bus import bus
from core.components.database.logic.db_service import Base, db_instance

from .models import ClusterLog

log = logging.getLogger("Core:Cluster:Logs")

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _level_names_at_least(min_level: str | None) -> list[str] | None:
    if not min_level:
        return None
    floor = LEVELS.get(str(min_level).upper())
    if floor is None:
        return None
    return [name for name, value in LEVELS.items() if value >= floor]


class ClusterLogHandler(logging.Handler):
    """Buffers formatted records for the shipper (thread-safe, bounded)."""

    def __init__(self, maxlen: int = 5000):
        super().__init__()
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.enabled = True
        self.exclude_exact: set[str] = {"uvicorn.access"}
        self.exclude_prefixes: tuple[str, ...] = ()
        self.dropped = 0

    def configure(self, *, enabled: bool, level: str, exclude: str) -> None:
        self.enabled = enabled
        self.setLevel(LEVELS.get(str(level).upper(), logging.INFO))
        names = [x.strip() for x in (exclude or "").split(",") if x.strip()]
        self.exclude_exact = {n for n in names if not n.endswith("*")}
        self.exclude_prefixes = tuple(n[:-1] for n in names if n.endswith("*"))

    def emit(self, record: logging.LogRecord) -> None:
        # Logger.callHandlers checks the level before calling us, Handler.handle()
        # (direct calls, tests) does not — enforce it here so the floor always holds.
        if not self.enabled or record.levelno < self.level:
            return
        name = record.name
        if name in self.exclude_exact or (self.exclude_prefixes and name.startswith(self.exclude_prefixes)):
            return
        try:
            message = self.format(record)
        except Exception:
            return
        with self._lock:
            if len(self._buf) == self._buf.maxlen:
                self.dropped += 1
            self._buf.append((name, record.levelname, message, datetime.utcnow()))

    def drain(self, max_items: int = 2000) -> list[tuple]:
        with self._lock:
            n = min(max_items, len(self._buf))
            items = [self._buf.popleft() for _ in range(n)]
        return items

    def pending(self) -> int:
        with self._lock:
            return len(self._buf)


cluster_log_handler = ClusterLogHandler()


class LogStore:
    """Ships the handler buffer to ``cluster_logs`` and serves queries from it."""

    def __init__(self, handler: ClusterLogHandler):
        self.handler = handler
        self.instance_id: str = "local"
        self.interval = 2.0
        self.retention_hours = 24
        self.max_per_instance = 5000
        self._is_leader: Callable[[], bool] = lambda: True
        self._task: Optional[asyncio.Task] = None
        self._schema_ready = False
        self.enabled = False
        self.shipped = 0
        self.dropped = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------- lifecycle
    def configure_from_settings(self) -> None:
        try:
            from config import settings

            self.enabled = bool(getattr(settings, "LYNDRIX_LOG_DB_ENABLED", True))
            self.retention_hours = int(getattr(settings, "LYNDRIX_LOG_DB_RETENTION_HOURS", 24))
            self.max_per_instance = int(getattr(settings, "LYNDRIX_LOG_DB_MAX_PER_INSTANCE", 5000))
            self.handler.configure(
                enabled=self.enabled,
                level=str(getattr(settings, "LYNDRIX_LOG_DB_LEVEL", "INFO")),
                exclude=str(getattr(settings, "LYNDRIX_LOG_DB_EXCLUDE", "uvicorn.access")),
            )
        except Exception as exc:  # settings not importable (tests)
            log.debug("log store: settings not applied: %s", exc)

    def start(self, instance_id: str, is_leader: Callable[[], bool]) -> None:
        """Start the shipper once the database is connected (idempotent)."""
        self.instance_id = instance_id or "local"
        self._is_leader = is_leader
        self.configure_from_settings()
        if not self.enabled:
            self.handler.enabled = False
            return
        if self._task is None or self._task.done():
            self._task = bus.create_tracked_task(self._run(), name="cluster:log_shipper")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        try:
            await self.flush()
        except Exception:
            pass

    def ensure_schema(self) -> bool:
        if self._schema_ready:
            return True
        engine = getattr(db_instance, "engine", None)
        if engine is None:
            return False
        lock = getattr(db_instance, "advisory_lock", None)
        cm = lock("lyndrix_bootstrap", timeout=60) if callable(lock) else _NullContext()
        with cm:
            try:
                Base.metadata.create_all(bind=engine, tables=[ClusterLog.__table__], checkfirst=True)
            except OperationalError as exc:
                if "1050" not in str(exc):
                    raise
        self._schema_ready = True
        return True

    async def _run(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(self.interval)
            try:
                if not self._schema_ready and not await asyncio.to_thread(self.ensure_schema):
                    continue
                await self.flush()
                ticks += 1
                if ticks % 90 == 0 and self._is_leader():  # ~every 3 min on the leader
                    await asyncio.to_thread(self.prune)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                log.debug("log store: shipper error: %s", exc)

    # -------------------------------------------------------------- shipping
    async def flush(self) -> int:
        items = self.handler.drain()
        if not items:
            return 0
        if not db_instance.is_connected or not db_instance.SessionLocal:
            self.dropped += len(items)
            return 0
        try:
            await asyncio.to_thread(self._insert, items)
        except Exception as exc:
            self.dropped += len(items)
            self.last_error = str(exc)[:200]
            return 0
        self.shipped += len(items)
        return len(items)

    def _insert(self, items: list[tuple]) -> None:
        with db_instance.SessionLocal() as session:
            session.add_all([
                ClusterLog(instance_id=self.instance_id, created_at=ts, source=name[:150],
                           level=level[:10], message=message)
                for name, level, message, ts in items
            ])
            session.commit()

    def prune(self) -> int:
        """Leader housekeeping: age retention + per-instance cap. Returns rows removed."""
        if not db_instance.SessionLocal:
            return 0
        removed = 0
        with db_instance.SessionLocal() as session:
            cutoff = datetime.utcnow() - timedelta(hours=max(1, self.retention_hours))
            res = session.execute(delete(ClusterLog).where(ClusterLog.created_at < cutoff))
            removed += res.rowcount or 0
            instance_ids = [r[0] for r in session.execute(select(ClusterLog.instance_id).distinct()).all()]
            for iid in instance_ids:
                threshold = session.execute(
                    select(ClusterLog.id).where(ClusterLog.instance_id == iid)
                    .order_by(ClusterLog.id.desc()).offset(self.max_per_instance).limit(1)
                ).scalar()
                if threshold is not None:
                    res = session.execute(
                        delete(ClusterLog).where(ClusterLog.instance_id == iid, ClusterLog.id <= threshold)
                    )
                    removed += res.rowcount or 0
            session.commit()
        return removed

    # --------------------------------------------------------------- queries
    def query(self, *, instance: str | None = None, source: str | None = None, limit: int = 200,
              min_level: str | None = None, since_id: int | None = None) -> list[dict]:
        """Newest ``limit`` lines (oldest first) of one instance or all of them."""
        if not db_instance.SessionLocal:
            return []
        limit = max(1, min(int(limit), 2000))
        with db_instance.SessionLocal() as session:
            q = select(ClusterLog.id, ClusterLog.instance_id, ClusterLog.created_at,
                       ClusterLog.source, ClusterLog.level, ClusterLog.message)
            if instance and instance != "all":
                q = q.where(ClusterLog.instance_id == instance)
            if source:
                q = q.where(ClusterLog.source == source)
            names = _level_names_at_least(min_level)
            if names is not None:
                q = q.where(ClusterLog.level.in_(names))
            if since_id:
                rows = session.execute(q.where(ClusterLog.id > int(since_id)).order_by(ClusterLog.id).limit(limit)).all()
            else:
                rows = session.execute(q.order_by(ClusterLog.id.desc()).limit(limit)).all()[::-1]
        return [
            {"id": int(i), "instance": iid, "ts": ts.isoformat() if ts else None,
             "source": src, "level": lvl, "message": msg}
            for i, iid, ts, src, lvl, msg in rows
        ]

    def sources(self, instance: str | None = None) -> list[str]:
        if not db_instance.SessionLocal:
            return []
        with db_instance.SessionLocal() as session:
            q = select(ClusterLog.source).distinct()
            if instance and instance != "all":
                q = q.where(ClusterLog.instance_id == instance)
            return sorted(r[0] for r in session.execute(q).all())

    def instances(self) -> list[dict]:
        """Instances that have shipped logs, newest first: ``{instance_id, last_ts, lines}``."""
        if not db_instance.SessionLocal:
            return []
        with db_instance.SessionLocal() as session:
            rows = session.execute(
                select(ClusterLog.instance_id, func.max(ClusterLog.created_at), func.count(ClusterLog.id))
                .group_by(ClusterLog.instance_id)
            ).all()
        out = [{"instance_id": iid, "last_ts": ts.isoformat() if ts else None, "lines": int(n)} for iid, ts, n in rows]
        return sorted(out, key=lambda r: r["last_ts"] or "", reverse=True)


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


log_store = LogStore(cluster_log_handler)
