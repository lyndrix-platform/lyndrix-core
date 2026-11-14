"""Shared helpers for cluster tests (imported as ``cluster_harness``; pytest puts
this directory on ``sys.path``).

Why a file database: coordinators run their SQL in worker threads
(``asyncio.to_thread``) while poll loops and heartbeats overlap. With
``sqlite://`` + ``StaticPool`` every thread shares ONE sqlite3 connection, and
returning a connection to the pool issues ``rollback()`` on it — the poll
thread's rollback then wipes the heartbeat/publish INSERT another thread has
not committed yet. That made ``test_db_coordinator`` fail every ~10th run
(instance row or event missing). A file database with the default pool gives
each thread its own connection, like the real MariaDB pool does.
"""
from __future__ import annotations

from sqlalchemy import create_engine


def make_engine(tmp_path, name: str = "cluster.db"):
    """File-backed SQLite engine that several coordinators (threads) can share."""
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        # Connections migrate between worker threads; writers wait for each
        # other instead of failing with "database is locked".
        connect_args={"check_same_thread": False, "timeout": 30},
    )
