"""Short write transactions that behave next to concurrent writers.

MariaDB ≥ 11.6 runs REPEATABLE READ with ``innodb_snapshot_isolation=ON``: a
transaction that updates/deletes a row another transaction changed since its
first read fails with error 1020 instead of waiting, and a writer blocked on a
row lock waits the server default of 50 s before 1205. Core services that do
small read-modify-write transactions from several instances therefore use:

* a READ COMMITTED session with a short ``innodb_lock_wait_timeout``
  (:func:`write_session`), and
* a bounded retry with a FRESH session and jittered backoff for the transient
  codes 1020 / 1205 / 1213 (:func:`run_with_retry`).

Everything here is blocking — call it from ``asyncio.to_thread``. On non-MySQL
dialects (SQLite in tests) the helpers degrade to a plain session.
"""
from __future__ import annotations

import random
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TypeVar

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from core.logger import get_logger

from .db_service import db_instance

log = get_logger("Core:DatabaseTx")

T = TypeVar("T")

# 1020 record changed since last read (snapshot isolation) · 1205 lock wait
# timeout · 1213 deadlock — all mean "run the transaction again".
RETRYABLE_MYSQL_CODES = frozenset({1020, 1205, 1213})
# 1050 table exists · 1060 duplicate column · 1061 duplicate key name: a peer
# instance won a bootstrap DDL race — the schema is there.
DDL_RACE_MYSQL_CODES = frozenset({1050, 1060, 1061})

_MYSQL_DIALECTS = ("mysql", "mariadb")


def is_mysql(bind=None) -> bool:
    """True when ``bind`` (default: the core engine) talks to MySQL/MariaDB."""
    bind = bind if bind is not None else db_instance.engine
    return bind is not None and getattr(bind.dialect, "name", "") in _MYSQL_DIALECTS


def mysql_error_code(exc: BaseException) -> Optional[int]:
    """Driver error code of a SQLAlchemy ``DBAPIError`` (``None`` if there is none)."""
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    code = args[0] if args else None
    return code if isinstance(code, int) else None


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError) and mysql_error_code(exc) in RETRYABLE_MYSQL_CODES


def is_ddl_race(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError) and mysql_error_code(exc) in DDL_RACE_MYSQL_CODES


@contextmanager
def write_session(lock_wait_seconds: int = 5) -> Iterator[Session]:
    """A session for one short write transaction (caller commits).

    MySQL/MariaDB: READ COMMITTED (no 1020 from snapshot isolation) and a row
    lock wait of ``lock_wait_seconds`` instead of 50 s, so a contended writer
    fails fast into the retry instead of parking a worker thread.

    The session is bound to ONE dedicated connection for its whole life. A
    plain session hands its connection back to the pool on every commit, so the
    session variable could neither be scoped to it nor reset reliably; here the
    lock wait is reset in ``finally`` before the connection returns to the pool
    (SQLAlchemy resets the isolation level itself on check-in).
    """
    if not is_mysql():
        session: Session = db_instance.SessionLocal()
        try:
            yield session
        finally:
            session.close()
        return

    conn = db_instance.engine.connect().execution_options(isolation_level="READ COMMITTED")
    session = None
    try:
        conn.exec_driver_sql(f"SET SESSION innodb_lock_wait_timeout = {int(lock_wait_seconds)}")
        # End the transaction the SET auto-began: the session must own (and be
        # able to commit) its transaction on this connection.
        conn.commit()
        session = db_instance.SessionLocal(bind=conn)
        yield session
    finally:
        try:
            if session is not None:
                session.close()  # rolls back whatever was not committed
            conn.exec_driver_sql("SET SESSION innodb_lock_wait_timeout = DEFAULT")
            conn.commit()
        except Exception as exc:
            # Broken connection: discard it rather than pool a tuned one.
            log.debug(f"DATABASE: could not reset lock wait timeout ({exc}) — invalidating connection")
            try:
                conn.invalidate()
            except Exception:
                pass
        finally:
            conn.close()


def run_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.1,
    retry_integrity: bool = False,
) -> T:
    """Run ``fn`` (which opens its OWN fresh session), retrying transient failures.

    Retries 1020/1205/1213 — and with ``retry_integrity`` also a duplicate-key
    ``IntegrityError`` (two writers inserting the same new row: the loser's
    second pass finds the row and updates it). Backoff is exponential with
    jitter so instances that collided do not collide again in lockstep.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            transient = is_retryable(exc) or (retry_integrity and isinstance(exc, IntegrityError))
            if not transient or attempt == attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)) * random.uniform(0.5, 1.5))
    raise RuntimeError("unreachable")  # pragma: no cover
