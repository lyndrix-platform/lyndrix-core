import asyncio
from contextlib import contextmanager
from typing import Iterator, Optional
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base
from core.bus import bus
from core.logger import get_logger
from config import settings

log = get_logger("Core:DatabaseService")
Base = declarative_base()

# Errors that indicate permanent misconfiguration (do not retry)
_PERMANENT_ERROR_MARKERS = [
    "Access denied",
    "Unknown database",
    "authentication failed",
    "no such host",
]


class DatabaseService:
    @staticmethod
    def _install_statement_cap(engine) -> None:
        """Cap statement time on every new pooled connection (see
        ``DB_STATEMENT_TIMEOUT_SECONDS``). MariaDB: ``max_statement_time`` (s,
        all statements); MySQL fallback: ``max_execution_time`` (ms, SELECT only).
        Failures are logged once and never block the connection."""
        seconds = int(getattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 0) or 0)
        if seconds <= 0 or engine.dialect.name not in ("mysql", "mariadb"):
            return
        warned = {"done": False}

        @event.listens_for(engine, "connect")
        def _cap(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            try:
                try:
                    cur.execute(f"SET SESSION max_statement_time = {seconds}")
                except Exception:
                    cur.execute(f"SET SESSION max_execution_time = {seconds * 1000}")
            except Exception as exc:
                if not warned["done"]:
                    warned["done"] = True
                    log.warning(f"DATABASE: could not cap statement time ({exc}) — running uncapped")
            finally:
                cur.close()

        log.info(f"DATABASE: statement time capped at {seconds}s per connection (DB_STATEMENT_TIMEOUT_SECONDS)")

    def __init__(self):
        self.engine = None
        self.SessionLocal = None
        self.is_connected = False
        self._connection_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._max_retries = 30  # ~2.5 minutes with 5s intervals
        # Boot-screen telemetry (read by BootService.snapshot): how far the
        # connection loop got, whether it gave up and why, and whether we were
        # ever connected (a watchdog drop then reads as "reconnecting").
        self.connect_attempts = 0
        self.failure_reason: Optional[str] = None  # "config_error" | "max_retries"
        self.was_connected = False
        bus.subscribe("vault:opened")(self.init_db_connection)

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @contextmanager
    def advisory_lock(self, name: str, timeout: int = 60) -> Iterator[bool]:
        """Serialize a critical section across core instances with MariaDB ``GET_LOCK``.

        Used around bootstrap DDL and seeds so two instances booting at the same
        time cannot race ``create_all``/``ALTER``/``INSERT`` (the loser used to
        hang in ``waiting_core`` forever). Held on a dedicated connection for the
        duration of the block. Yields ``True`` when the lock was obtained,
        ``False`` on timeout or on non-MySQL dialects (SQLite tests) — callers
        proceed either way; the lock only narrows the race window.
        """
        engine = self.engine
        if engine is None or engine.dialect.name not in ("mysql", "mariadb"):
            yield False
            return
        cap = int(getattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 0) or 0)
        if cap > 0:
            timeout = max(1, min(timeout, cap - 5))  # GET_LOCK waiting counts as statement time
        conn = engine.connect()
        acquired = False
        try:
            try:
                acquired = bool(conn.execute(text("SELECT GET_LOCK(:n, :t)"), {"n": name, "t": timeout}).scalar())
            except Exception as exc:
                log.warning(f"DATABASE: GET_LOCK('{name}') failed ({exc}) — continuing unlocked")
            if not acquired:
                log.warning(f"DATABASE: could not obtain lock '{name}' within {timeout}s — continuing unlocked")
            yield acquired
        finally:
            if acquired:
                try:
                    conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": name})
                except Exception:
                    pass
            conn.close()

    async def init_db_connection(self, payload=None):
        # ``vault:opened`` fires repeatedly (reconnects, multiple vault code
        # paths). If an engine is already live or a connection loop is in flight,
        # this is a no-op — recreating would leak the previous pool and bounce a
        # healthy connection. Only (re)initialize when there is no active engine
        # or the previous connection attempt has finished (e.g. exhausted).
        loop_active = self._connection_task is not None and not self._connection_task.done()
        if self.engine is not None and (self.is_connected or loop_active):
            log.debug("DATABASE: engine already initialized; ignoring duplicate vault:opened.")
            return
        if self.engine is not None:
            # Stale engine from a prior failed/exhausted attempt — dispose its
            # pool before creating a fresh one so connections are not leaked.
            try:
                self.engine.dispose()
            except Exception as e:
                log.debug(f"DATABASE: prior engine dispose failed: {e}")

        log.info(f"DATABASE: Vault is open. Initializing engine for {settings.DATABASE_URL_SAFE}")
        try:
            recycle = int(getattr(settings, "DB_POOL_RECYCLE_SECONDS", 0) or 0)
            self.engine = create_engine(
                settings.DATABASE_URL,
                pool_pre_ping=True,
                # Proxies (MaxScale/ProxySQL) and Galera nodes drop idle
                # connections; recycle ours before they do (0 → never).
                pool_recycle=recycle if recycle > 0 else -1,
                connect_args={'connect_timeout': 5}
            )
            self._install_statement_cap(self.engine)
            self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

            self._connection_task = bus.create_tracked_task(
                self._connection_loop(),
                name="db_service:connection_loop"
            )
        except Exception as e:
            log.error(f"CRITICAL: Engine initialization failed: {e}")

    async def _connection_loop(self):
        """Retries DB connection, distinguishing transient from permanent failures."""
        log.info(f"CONNECT: Attempting connection to {settings.DB_HOST}...")
        attempt = 0
        self.failure_reason = None

        reconnecting = self.was_connected
        while not self.is_connected:
            attempt += 1
            self.connect_attempts = attempt
            try:
                await asyncio.to_thread(self._check_db_sync)

                self.is_connected = True
                self.was_connected = True
                log.info("SUCCESS: Database connection established.")
                if reconnecting:
                    # Reconnect after an outage (DB restart, MaxScale failover):
                    # peers that only need "the DB is back" listen here; the
                    # legacy db:connected still fires for plugins that re-run
                    # their table checks on it. The boot service ignores the
                    # repeated iam:ready so modules are NOT loaded twice.
                    bus.emit("db:reconnected", {"status": "ready", "attempts": attempt})
                bus.emit("db:connected", {"status": "ready"})
                bus.emit("system:maintenance_mode", {"service": "db", "active": False})

                self._watchdog_task = bus.create_tracked_task(
                    self._watchdog(),
                    name="db_service:watchdog"
                )
                break

            except Exception as e:
                error_str = str(e)

                # Check if this is a permanent config/credential error
                if any(marker.lower() in error_str.lower() for marker in _PERMANENT_ERROR_MARKERS):
                    log.error(f"PERMANENT: Database configuration error (not retrying): {self._redact_error(error_str)}")
                    self.failure_reason = "config_error"
                    bus.emit("system:maintenance_mode", {"service": "db", "active": True, "reason": "config_error"})
                    return

                if attempt >= self._max_retries and not reconnecting:
                    log.error(f"EXHAUSTED: Database connection failed after {attempt} attempts. Giving up.")
                    self.failure_reason = "max_retries"
                    bus.emit("system:maintenance_mode", {"service": "db", "active": True, "reason": "max_retries"})
                    return

                if reconnecting:
                    # Never give up on a connection we once had: a DB/proxy failover
                    # or maintenance window must not turn into a dead core instance.
                    delay = min(30, 5 * (1 + attempt // 6))
                    if attempt in (1, 6, 12) or attempt % 60 == 0:
                        log.warning(f"RETRY: Database connection lost — reconnect attempt {attempt}, next in {delay}s")
                    if attempt == 1:
                        bus.emit("system:maintenance_mode", {"service": "db", "active": True, "reason": "reconnecting"})
                    await asyncio.sleep(delay)
                    continue

                log.warning(f"RETRY: Database not reachable (attempt {attempt}/{self._max_retries}). Retrying in 5s...")
                await asyncio.sleep(5)

    def _check_db_sync(self):
        """Synchronous helper for DB health check (runs in executor)."""
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    async def _watchdog(self):
        """Monitors the connection in the background."""
        while self.is_connected:
            await asyncio.sleep(15)
            try:
                await asyncio.to_thread(self._check_db_sync)
            except Exception as e:
                log.error(f"LOST: Database connection failed: {self._redact_error(str(e))}")
                self.is_connected = False
                self._connection_task = bus.create_tracked_task(
                    self._connection_loop(),
                    name="db_service:reconnect"
                )
                break

    @staticmethod
    def _redact_error(error_str: str) -> str:
        """Removes potential credentials from error messages."""
        import re
        # Redact anything that looks like a connection string password
        redacted = re.sub(r'://[^:]+:[^@]+@', '://***:***@', error_str)
        return redacted


db_instance = DatabaseService()