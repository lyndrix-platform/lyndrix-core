"""Tables of the ``db`` coordinator driver (docs/ha/building-blocks/coordination.md).

All timestamps are naive UTC **taken from the database clock** — instances never
compare their own wall clock against these columns, so clock drift between
instances cannot steal or extend a lease.
"""
from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String, Text

from core.components.database.logic.db_service import Base

CLUSTER_TABLES = ("cluster_instances", "cluster_leases", "cluster_kv", "cluster_events", "cluster_logs")


class ClusterInstance(Base):
    """One row per live core instance, refreshed by its heartbeat."""

    __tablename__ = "cluster_instances"

    instance_id = Column(String(100), primary_key=True)
    hostname = Column(String(255), nullable=True)
    roles = Column(String(100), nullable=False, default="api")
    core_version = Column(String(32), nullable=True)
    api_version = Column(String(32), nullable=True)
    started_at = Column(DateTime, nullable=False)
    heartbeat_at = Column(DateTime, nullable=False)
    # ready/draining flags, secret fingerprint, plugin fingerprint, pid, …
    meta = Column(JSON, nullable=True)


class ClusterLease(Base):
    """Named lease (``core:leader``, ``monitoring:prober``, ``plugin:<id>:singleton``)."""

    __tablename__ = "cluster_leases"

    name = Column(String(150), primary_key=True)
    holder = Column(String(100), nullable=True)
    acquired_at = Column(DateTime, nullable=True)
    renewed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)


class ClusterKV(Base):
    """Small TTL key/value store (OIDC state, SSO codes, rate-limiter counters)."""

    __tablename__ = "cluster_kv"

    k = Column(String(190), primary_key=True)
    v = Column(JSON, nullable=True)
    expires_at = Column(DateTime, nullable=True)


class ClusterEvent(Base):
    """Append-only event log that fans bridged bus topics out to every instance."""

    __tablename__ = "cluster_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    topic = Column(String(150), nullable=False)
    payload = Column(JSON, nullable=True)
    origin = Column(String(100), nullable=False)
    created_at = Column(DateTime, nullable=False, index=True)


class ClusterLog(Base):
    """Central log: every instance ships its recent log lines here so the UI can
    show the log of *any* instance (or all of them merged) from whichever
    instance answers the request. The in-memory ring buffer stays the fast path
    for the local instance; this table survives pod restarts and is pruned by
    the leader (age + per-instance cap, see LYNDRIX_LOG_DB_*)."""

    __tablename__ = "cluster_logs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    instance_id = Column(String(100), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, index=True)
    source = Column(String(150), nullable=False, index=True)
    level = Column(String(10), nullable=False)
    message = Column(Text, nullable=False)
