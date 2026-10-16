"""Shared notification store (core 1.8).

Before 1.8 every instance kept its own ``notifications.json`` — in a cluster
the bell showed whatever the answering pod had recorded. These two tables hold
the history and the per-user read/dismiss state for all instances; the file
stays the fallback until the database is connected (and is imported once).
"""
from sqlalchemy import Boolean, Column, Float, String, Text

from core.components.database.logic.db_service import Base


class NotificationRow(Base):
    __tablename__ = "notifications"

    id = Column(String(64), primary_key=True)
    timestamp = Column(Float, nullable=False, index=True)
    title = Column(String(255), nullable=True)
    message = Column(Text, nullable=True)
    type = Column(String(32), nullable=True)
    user_id = Column(String(100), nullable=True, index=True)       # None = broadcast
    required_permission = Column(String(200), nullable=True)
    source_plugin_id = Column(String(150), nullable=True)
    endpoint_name = Column(String(100), nullable=True)


class NotificationUserState(Base):
    __tablename__ = "notification_user_states"

    user_id = Column(String(100), primary_key=True)
    notification_id = Column(String(64), primary_key=True)
    read = Column(Boolean, nullable=False, default=False)
    dismissed = Column(Boolean, nullable=False, default=False)
