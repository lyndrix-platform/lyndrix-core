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

import time
from datetime import datetime, timedelta

from sqlalchemy import create_engine

import core.services  # noqa: F401  — must be imported before core.api (circular import)
from core.components.cluster.logic.cluster_service import ClusterService
from core.components.cluster.logic.drivers.db import DbCoordinator


def make_engine(tmp_path, name: str = "cluster.db"):
    """File-backed SQLite engine that several coordinators (threads) can share."""
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        # Connections migrate between worker threads; writers wait for each
        # other instead of failing with "database is locked".
        connect_args={"check_same_thread": False, "timeout": 30},
    )


class FakeClock:
    """Monotonic clock under test control (LeaseKeeper / ClusterService ``clock``)."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DbClock:
    """The "database clock" (naive UTC) for ``DbCoordinator(now_fn=…)``. Give all
    coordinators of a test the same instance: they then agree on database time
    exactly like instances sharing one MariaDB."""

    def __init__(self, start: datetime = datetime(2026, 11, 14, 12, 0, 0)):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ClusterClock:
    """One knob for both clocks: ``advance()`` moves the database clock and the
    instances' monotonic clocks together (use ``.mono`` / ``.db`` to drift them
    apart on purpose, e.g. to model a paused instance)."""

    def __init__(self):
        self.mono, self.db = FakeClock(), DbClock()

    def advance(self, seconds: float) -> None:
        self.mono.advance(seconds)
        self.db.advance(seconds)


class FaultyDbCoordinator(DbCoordinator):
    """DbCoordinator whose database can be broken per instance:

    * ``fail = SomeError(...)`` — every call raises it (database unreachable);
    * ``delay = seconds`` — every call blocks that long first (slow / hung
      database; the call runs in the driver's worker thread, so the event loop
      stays free, as with a real dead socket).
    Reset both to ``None`` / ``0`` to heal it.
    """

    fail: Exception | None = None
    delay: float = 0.0

    def _engine(self):
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return super()._engine()


def make_coordinator(engine, instance_id: str, *, clock: ClusterClock | None = None, roles=("api", "leader"),
                     ttl: float = 30, heartbeat_s: float = 10, grace_s: float = 90,
                     cls=FaultyDbCoordinator) -> DbCoordinator:
    coord = cls(instance_id=instance_id, ttl=ttl, heartbeat_s=heartbeat_s, poll_ms=100,
                engine_getter=lambda: engine, now_fn=clock.db if clock else None,
                restart_grace_s=grace_s, roles=list(roles))
    coord.ensure_schema()
    return coord


def make_service(engine, instance_id: str, *, clock: ClusterClock | None = None, roles=("api", "leader"),
                 ttl: float = 30, heartbeat_s: float = 10, grace_s: float = 90) -> ClusterService:
    """A cluster-mode ``ClusterService`` on the shared test database — one per
    simulated instance. It does not subscribe to the bus and does not attach
    the bridge (both are process-global); drive it explicitly::

        await svc._start_once()        # coordinator + leases + first heartbeat
        await svc._heartbeat_once()    # one beat: registry row, peer view, leader
        await svc.leases.tick()        # one lease pass
        svc.begin_drain(); await svc.stop()

    Readiness reads the process-global boot/database/Vault flags — patch them
    once per test (see ``ready_core`` in ``test_instance_lifecycle``).
    """
    coord = make_coordinator(engine, instance_id, clock=clock, roles=roles, ttl=ttl,
                             heartbeat_s=heartbeat_s, grace_s=grace_s)
    return ClusterService(
        instance_id=instance_id, roles=list(roles), mode="cluster", driver="db", ttl=ttl,
        heartbeat_s=heartbeat_s, drain_seconds=0, restart_grace_s=grace_s, coordinator=coord,
        clock=clock.mono if clock else None, subscribe=False, attach_bridge=False,
    )
