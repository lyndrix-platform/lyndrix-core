"""``write_session`` / ``run_with_retry`` (shared by the BG-03 / BG-21 fixes).

The MySQL branch cannot run against SQLite, so it is checked with a recording
fake: READ COMMITTED + short lock wait on ONE dedicated connection, and the
session variable is reset before the connection goes back to the pool.
"""
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

import core.services  # noqa: F401
from core.components.database.logic import tx


class FakeConn:
    def __init__(self, log, fail_reset=False):
        self.log, self.fail_reset = log, fail_reset

    def execution_options(self, **opts):
        self.log.append(("options", opts))
        return self

    def exec_driver_sql(self, sql):
        if self.fail_reset and "DEFAULT" in sql:
            raise RuntimeError("connection lost")
        self.log.append(("sql", sql))

    def commit(self):
        self.log.append(("commit",))

    def invalidate(self):
        self.log.append(("invalidate",))

    def close(self):
        self.log.append(("close",))


class FakeSession:
    def __init__(self, log):
        self.log = log

    def close(self):
        self.log.append(("session.close",))


def _fake_mysql(monkeypatch, log, **conn_kwargs):
    conn = FakeConn(log, **conn_kwargs)

    class Engine:
        dialect = type("D", (), {"name": "mariadb"})()

        def connect(self):
            return conn

    def session_local(bind=None):
        assert bind is conn          # the session lives on the tuned connection
        return FakeSession(log)

    monkeypatch.setattr(tx.db_instance, "engine", Engine(), raising=False)
    monkeypatch.setattr(tx.db_instance, "SessionLocal", session_local, raising=False)


def test_mysql_session_is_read_committed_with_short_lock_wait_and_resets(monkeypatch):
    log: list = []
    _fake_mysql(monkeypatch, log)

    with pytest.raises(ValueError):
        with tx.write_session(lock_wait_seconds=7):
            log.append(("body",))
            raise ValueError("boom")     # reset must happen on errors too

    assert log == [
        ("options", {"isolation_level": "READ COMMITTED"}),
        ("sql", "SET SESSION innodb_lock_wait_timeout = 7"),
        ("commit",),
        ("body",),
        ("session.close",),
        ("sql", "SET SESSION innodb_lock_wait_timeout = DEFAULT"),
        ("commit",),
        ("close",),
    ]


def test_connection_that_cannot_be_reset_is_invalidated(monkeypatch):
    log: list = []
    _fake_mysql(monkeypatch, log, fail_reset=True)
    with tx.write_session():
        pass
    assert log[-2:] == [("invalidate",), ("close",)]


class _DriverError(Exception):
    pass


def _op(code):
    return OperationalError("stmt", {}, _DriverError(code, "x"))


def test_run_with_retry_retries_transient_codes_only(monkeypatch):
    monkeypatch.setattr(tx.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _op(1213)
        return "done"

    assert tx.run_with_retry(flaky) == "done" and calls["n"] == 3

    calls["n"] = 0

    def permanent():
        calls["n"] += 1
        raise _op(1064)                  # syntax error: not transient

    with pytest.raises(OperationalError):
        tx.run_with_retry(permanent)
    assert calls["n"] == 1

    def always():
        raise _op(1205)

    with pytest.raises(OperationalError):
        tx.run_with_retry(always, attempts=3)


def test_run_with_retry_integrity_is_opt_in(monkeypatch):
    monkeypatch.setattr(tx.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def dup_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT", {}, _DriverError(1062, "Duplicate entry"))
        return calls["n"]

    with pytest.raises(IntegrityError):
        tx.run_with_retry(dup_then_ok)
    calls["n"] = 0
    assert tx.run_with_retry(dup_then_ok, retry_integrity=True) == 2
