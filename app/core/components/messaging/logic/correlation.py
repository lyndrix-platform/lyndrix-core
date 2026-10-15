"""
CorrelationStore — two-layer storage for pending interactive actions.

Layer 1: in-memory dict for sub-millisecond lookup at runtime.
Layer 2: DB write-through (``PendingActionRecord``) so pending actions
         survive process restarts.

Usage by an originating plugin::

    correlation_id = uuid4()
    await correlation_store.create(
        correlation_id=correlation_id,
        source_plugin_id="lyndrix.plugin.state_monitoring",
        resume_topic="monitoring:action_response",
        state_snapshot={"check_id": 42, "host": "prod-web-01"},
        ttl_seconds=3600,
    )
    # Attach correlation_id to OutboundMessage and emit messaging:outbound.
    # When the user clicks a button, the Gateway calls resolve() and the
    # resume_topic event fires automatically.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import update

from core.components.database.logic.db_service import db_instance
from core.components.messaging.model.schemas import InboundMessage
from core.components.messaging.model.pending_action import PendingActionRecord

log = logging.getLogger("Core:CorrelationStore")


class PendingAction(BaseModel):
    correlation_id:   UUID
    source_plugin_id: str
    resume_topic:     str
    state_snapshot:   dict
    expires_at:       datetime


class CorrelationStore:
    """Manages the lifecycle of correlation IDs for interactive messages."""

    def __init__(self) -> None:
        self._cache: dict[str, PendingAction] = {}

    # ------------------------------------------------------------------
    # DB initialisation (called on db:connected)
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        """Create the table if needed and reload unresolved rows into cache."""
        session = self._session()
        if not session:
            return
        try:
            # Create table (idempotent — checkfirst=True)
            PendingActionRecord.__table__.create(
                bind=db_instance.engine, checkfirst=True
            )
            # Reload unresolved, non-expired rows
            now = datetime.now(timezone.utc)
            rows = (
                session.query(PendingActionRecord)
                .filter(
                    PendingActionRecord.resolved_at.is_(None),
                    PendingActionRecord.expires_at > now,
                )
                .all()
            )
            for row in rows:
                pa = PendingAction(
                    correlation_id=UUID(row.correlation_id),
                    source_plugin_id=row.source_plugin_id,
                    resume_topic=row.resume_topic,
                    state_snapshot=row.state_json or {},
                    expires_at=row.expires_at,
                )
                self._cache[row.correlation_id] = pa
            log.info("CorrelationStore: loaded %d pending action(s) from DB.", len(rows))
        except Exception as exc:
            log.error("CorrelationStore: init_db failed: %s", exc)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        correlation_id: UUID,
        source_plugin_id: str,
        resume_topic: str,
        state_snapshot: dict,
        ttl_seconds: int = 3600,
    ) -> PendingAction:
        """Register a new pending action and persist it to the DB."""
        now      = datetime.now(timezone.utc)
        expires  = now + timedelta(seconds=ttl_seconds)
        pa       = PendingAction(
            correlation_id=correlation_id,
            source_plugin_id=source_plugin_id,
            resume_topic=resume_topic,
            state_snapshot=state_snapshot,
            expires_at=expires,
        )
        # Write to in-memory cache
        self._cache[str(correlation_id)] = pa

        # Write-through to DB off the event loop (sync SQLAlchemy).
        def _write() -> None:
            session = self._session()
            if not session:
                return
            try:
                record = PendingActionRecord(
                    correlation_id=str(correlation_id),
                    source_plugin_id=source_plugin_id,
                    resume_topic=resume_topic,
                    state_json=state_snapshot,
                    expires_at=expires,
                    created_at=now,
                )
                session.merge(record)
                session.commit()
            except Exception as exc:
                log.warning("CorrelationStore.create: DB write failed: %s", exc)
                session.rollback()
            finally:
                session.close()

        await asyncio.to_thread(_write)
        return pa

    @staticmethod
    def _row_to_action(record: PendingActionRecord) -> PendingAction:
        return PendingAction(
            correlation_id=UUID(record.correlation_id),
            source_plugin_id=record.source_plugin_id,
            resume_topic=record.resume_topic,
            state_snapshot=record.state_json or {},
            expires_at=record.expires_at,
        )

    async def resolve(
        self, correlation_id: UUID, response: InboundMessage
    ) -> PendingAction | None:
        """Mark a correlation as resolved — exactly once, cluster-wide.

        The message may have been sent by another core instance (its cache has
        the entry, ours does not), and the provider may deliver the click twice or
        to two instances. The database row is therefore the arbiter: one
        conditional ``UPDATE … WHERE resolved_at IS NULL`` — whoever gets
        ``rowcount == 1`` handles the action, everyone else drops it. The local
        cache only speeds up the common single-instance path.
        """
        key = str(correlation_id)
        cached = self._cache.pop(key, None)

        def _claim() -> tuple[str, PendingAction | None]:
            session = self._session()
            if not session:
                return "nodb", None
            try:
                now = datetime.now(timezone.utc)
                res = session.execute(
                    update(PendingActionRecord)
                    .where(
                        PendingActionRecord.correlation_id == key,
                        PendingActionRecord.resolved_at.is_(None),
                        PendingActionRecord.expires_at > now,
                    )
                    .values(resolved_at=now)
                )
                session.commit()
                if res.rowcount != 1:
                    return "lost", None
                record = session.get(PendingActionRecord, key)
                return "won", (self._row_to_action(record) if record else None)
            except Exception as exc:
                log.warning("CorrelationStore.resolve: DB claim failed: %s", exc)
                session.rollback()
                return "error", None
            finally:
                session.close()

        status, from_db = await asyncio.to_thread(_claim)
        if status == "won":
            return from_db or cached
        if status == "lost":
            if cached is not None:
                log.info("CorrelationStore.resolve: %s was already resolved on another instance.", key)
            else:
                log.warning("CorrelationStore.resolve: unknown or expired correlation_id %s", key)
            return None
        # No database (tests, DB blip): fall back to the local cache.
        if cached is None:
            log.warning("CorrelationStore.resolve: unknown correlation_id %s", key)
        return cached

    async def get(self, correlation_id: UUID) -> PendingAction | None:
        """Look up a pending action without resolving it (cache, then database —
        the message may have been sent from another instance)."""
        key = str(correlation_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        def _read() -> PendingAction | None:
            session = self._session()
            if not session:
                return None
            try:
                record = session.get(PendingActionRecord, key)
                if not record or record.resolved_at is not None:
                    return None
                expires = record.expires_at
                if expires is not None and expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires is not None and expires <= datetime.now(timezone.utc):
                    return None
                return self._row_to_action(record)
            except Exception as exc:
                log.debug("CorrelationStore.get: DB read failed: %s", exc)
                return None
            finally:
                session.close()

        return await asyncio.to_thread(_read)

    async def expire_stale(self) -> int:
        """Remove expired entries from cache and DB. Returns count removed."""
        now   = datetime.now(timezone.utc)
        stale = [k for k, pa in list(self._cache.items()) if pa.expires_at <= now]
        for k in stale:
            self._cache.pop(k, None)

        # Delete expired rows off the event loop (sync SQLAlchemy).
        def _delete() -> int:
            session = self._session()
            if not session:
                return 0
            try:
                count = (
                    session.query(PendingActionRecord)
                    .filter(
                        PendingActionRecord.resolved_at.is_(None),
                        PendingActionRecord.expires_at <= now,
                    )
                    .delete(synchronize_session=False)
                )
                session.commit()
                return count
            except Exception as exc:
                log.warning("CorrelationStore.expire_stale: %s", exc)
                session.rollback()
                return 0
            finally:
                session.close()

        db_count = await asyncio.to_thread(_delete)
        total = len(stale) + db_count
        if total:
            log.info("CorrelationStore: expired %d stale pending action(s).", total)
        return total

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session(self):
        if not db_instance.is_connected or not db_instance.SessionLocal:
            return None
        return db_instance.SessionLocal()
