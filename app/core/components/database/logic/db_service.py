import asyncio
from typing import Optional
from sqlalchemy import create_engine, text
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
    def __init__(self):
        self.engine = None
        self.SessionLocal = None
        self.is_connected = False
        self._connection_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._max_retries = 30  # ~2.5 minutes with 5s intervals
        bus.subscribe("vault:opened")(self.init_db_connection)

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
            self.engine = create_engine(
                settings.DATABASE_URL,
                pool_pre_ping=True,
                connect_args={'connect_timeout': 5}
            )
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

        while not self.is_connected:
            attempt += 1
            try:
                await asyncio.to_thread(self._check_db_sync)

                self.is_connected = True
                log.info("SUCCESS: Database connection established.")
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
                    bus.emit("system:maintenance_mode", {"service": "db", "active": True, "reason": "config_error"})
                    return

                if attempt >= self._max_retries:
                    log.error(f"EXHAUSTED: Database connection failed after {attempt} attempts. Giving up.")
                    bus.emit("system:maintenance_mode", {"service": "db", "active": True, "reason": "max_retries"})
                    return

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