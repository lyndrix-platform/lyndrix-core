"""Notification write-through to the shared store (audit finding BG-05).

* A change that arrives while a flush is running in its worker thread must be
  written by the same flush task (it used to stay queued until some later
  change — possibly never).
* A transient database error must re-queue the batch instead of dropping it.
"""
import asyncio
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import tx as tx_mod
from core.components.database.logic.db_service import db_instance
from core.components.notifications.logic import notification_service as ns_mod
from core.components.notifications.model.models import NotificationRow, NotificationUserState


@pytest.fixture()
def svc(monkeypatch):
    service = ns_mod.NotificationService()
    service._db_enabled = True
    monkeypatch.setattr(ns_mod, "_SAVE_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(ns_mod, "_DB_FLUSH_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(tx_mod.time, "sleep", lambda _s: None)
    return service


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    NotificationRow.__table__.create(bind=engine)
    NotificationUserState.__table__.create(bind=engine)
    monkeypatch.setattr(db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    return engine


class _DriverError(Exception):
    pass


def _lock_wait_timeout() -> OperationalError:
    return OperationalError("UPDATE …", {}, _DriverError(1205, "Lock wait timeout exceeded"))


def test_change_during_a_running_flush_is_written(svc):
    flushed: list[str] = []

    def slow_flush() -> bool:
        with svc._db_lock:
            pending, svc._db_pending = svc._db_pending, []
        time.sleep(0.2)                       # DB round trip / lock wait
        flushed.extend(pending)
        return True

    svc._db_flush = slow_flush

    async def run():
        svc._db_op("A")
        svc._request_save()
        await asyncio.sleep(0.08)             # the flush of A is running in its thread
        svc._db_op("B")
        svc._request_save()                   # arrives during the flush
        await svc._save_task

    asyncio.run(run())
    assert flushed == ["A", "B"]
    assert svc._db_pending == []


def test_transient_error_requeues_in_order_and_is_flushed_later(svc, db, monkeypatch):
    svc._db_user_state("alice", ["n-1"], read=True)
    svc._db_user_state("alice", ["n-2"], dismissed=True)
    failing = {"on": True}
    real_session = tx_mod.write_session

    def flaky_session(*args, **kwargs):
        if failing["on"]:
            raise _lock_wait_timeout()
        return real_session(*args, **kwargs)

    monkeypatch.setattr(tx_mod, "write_session", flaky_session)

    assert svc._db_flush() is False           # all attempts hit the lock wait
    assert len(svc._db_pending) == 2          # nothing dropped
    svc._db_user_state("alice", ["n-3"], read=True)   # queued meanwhile → stays behind the batch

    failing["on"] = False
    assert svc._db_flush() is True
    with sessionmaker(bind=db)() as s:
        states = {r.notification_id: (r.read, r.dismissed) for r in s.query(NotificationUserState)}
    assert states == {"n-1": (True, False), "n-2": (False, True), "n-3": (True, False)}
    assert svc._db_pending == []


def test_flush_task_retries_a_requeued_batch_by_itself(svc, db, monkeypatch):
    calls = {"n": 0}
    real_session = tx_mod.write_session

    def flaky_session(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 3:                   # the whole first flush (3 attempts) fails
            raise _lock_wait_timeout()
        return real_session(*args, **kwargs)

    monkeypatch.setattr(tx_mod, "write_session", flaky_session)

    async def run():
        svc._db_user_state("bob", ["n-9"], read=True)
        svc._request_save()
        await svc._save_task                  # no further change needed to get it written

    asyncio.run(run())
    with sessionmaker(bind=db)() as s:
        assert s.query(NotificationUserState).count() == 1


def test_broken_operation_is_dropped_not_retried_forever(svc, db):
    def broken(_session):
        raise ValueError("bug in an operation")

    svc._db_op(broken)
    assert svc._db_flush() is True
    assert svc._db_pending == []


def test_backlog_is_bounded_when_the_database_stays_down(svc, db, monkeypatch):
    monkeypatch.setattr(ns_mod, "_DB_PENDING_LIMIT", 5)

    def down(*args, **kwargs):
        raise _lock_wait_timeout()

    monkeypatch.setattr(tx_mod, "write_session", down)
    for i in range(8):
        svc._db_op(i)
    assert svc._db_flush() is False
    assert svc._db_pending == [3, 4, 5, 6, 7]  # newest kept
