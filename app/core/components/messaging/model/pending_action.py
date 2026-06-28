"""
SQLAlchemy model for persistent PendingAction storage.

Pending actions survive process restarts: on boot the CorrelationStore
loads unresolved rows from this table into its in-memory cache.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, JSON, String

from core.components.database.logic.db_service import Base


class PendingActionRecord(Base):
    """One row per outstanding correlation-ID waiting for a human response."""

    __tablename__ = "messaging_pending_actions"

    correlation_id   = Column(String(36),  primary_key=True)
    source_plugin_id = Column(String(100), nullable=False)
    # EventBus topic the Gateway will emit when this action is resolved
    resume_topic     = Column(String(200), nullable=False)
    # Arbitrary state snapshot the originating plugin provided for resumption
    state_json       = Column(JSON,        nullable=False, default=dict)
    expires_at       = Column(DateTime(timezone=True), nullable=False)
    created_at       = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # Set when a user response has been received and routed
    resolved_at      = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Speeds up the expire_stale() query which filters on both columns.
        Index("ix_pending_resolved_expires", "resolved_at", "expires_at"),
    )
