"""``db`` driver — coordination through the shared MariaDB (docs/ha/building-blocks/coordination.md).

Why the database and not Redis first: HA must not introduce a new mandatory
dependency that itself has to be made highly available; the DB is already
there and already HA in a Tier-2 deployment. Lyndrix's event rate is tiny
(runtime status coalesced to 1/s, notifications, plugin state changes), so a
500 ms poll on ``cluster_events`` is cheap.

Rules this driver follows:

* **DB clock only.** Every comparison uses ``UTC_TIMESTAMP()`` (MariaDB) /
  ``strftime('now')`` (SQLite). Instances never mix their own wall clock in.
* **Short READ COMMITTED transactions + retry on 1020/1205/1213.** MariaDB
  ≥ 11.6 runs with ``innodb_snapshot_isolation=ON``: a REPEATABLE READ
  transaction that updates a row changed since its snapshot fails with error
  1020 instead of waiting — exactly the race two instances renewing a lease
  produce. READ COMMITTED sidesteps it; the retry covers deadlocks/lock waits.
* **Atomic lease acquire/renew** in one ``UPDATE … WHERE holder = me OR expired``;
  the first holder is created with an ``INSERT`` whose ``IntegrityError`` means
  "somebody else was faster". The same statement assigns the **fencing token**:
  kept on an uninterrupted renewal, larger than every earlier one on a new
  acquisition. A released lease keeps its row (``holder = NULL``) so the token
  sequence survives the release.
* **Own threads.** The blocking SQL runs on a small executor of its own, not on
  the loop's shared default executor: coordinator calls hanging on a dead
  database socket cannot use up the threads the rest of the process needs
  (request handlers, Vault, logins), and vice versa.
* **Events are append-only** and read by ``id > last_seen``; a fresh instance
  starts at ``MAX(id)`` so it never replays history. The leader prunes rows
  older than one hour and instances that stopped heart-beating.

Every method is ``async`` and runs its blocking SQL in a worker thread.

Mixed versions during a rolling update: schema changes are additive (nullable
columns added under the bootstrap lock). Instances of core ≤ 1.11 ignore the new
columns, still ``DELETE`` a lease row on release and take over expired leases
without touching ``token`` — the token of the next acquisition by a newer
instance is then derived from the database clock (see ``_next_token``), so it
still exceeds every token issued before.
"""
from __future__ import annotations

import asyncio
import calendar
import functools
import json
import random
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import and_, case, delete, func, insert, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from core.bus import bus
from core.logger import get_logger

from ..coordinator import EventCallback, InstanceInfo, LeaseInfo
from ..models import ClusterEvent, ClusterInstance, ClusterKV, ClusterLease

log = get_logger("Core:Cluster:DB")

# MariaDB error codes worth a retry: 1020 record changed (snapshot isolation),
# 1205 lock wait timeout, 1213 deadlock.
_RETRY_CODES = (1020, 1205, 1213)
_EVENT_RETENTION = timedelta(hours=1)
_PRUNE_INTERVAL_S = 300
_EVENT_BATCH = 500
# Released lease rows are kept (they carry the token counter); names nobody
# asked for in a day are dropped so one-off lease names cannot pile up.
_RELEASED_LEASE_RETENTION = timedelta(hours=24)
# Worker threads for coordinator SQL. Sized for: one renewal per lease (twice
# while calls hang), heartbeat, event poll, publisher, kv calls of requests.
_EXECUTOR_THREADS = 16
# Columns added after the tables first shipped (core 1.4) — all nullable, so
# older instances that never mention them keep inserting and updating rows.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "cluster_leases": {"token": "BIGINT NULL"},
}
# A metadata lock the ALTER cannot get within this many seconds is not waited
# for: the pending ALTER would block every other statement on the table.
_DDL_LOCK_WAIT_S = 5


def _mysql_errno(exc: BaseException) -> Optional[int]:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    return args[0] if args and isinstance(args[0], int) else None


def _epoch_micros(moment: datetime) -> int:
    """Naive UTC datetime → microseconds since the epoch (token floor)."""
    return calendar.timegm(moment.timetuple()) * 1_000_000 + moment.microsecond


def _json_safe(payload: dict) -> dict:
    """Bus payloads may carry datetimes/UUIDs — JSON columns need plain data."""
    try:
        return json.loads(json.dumps(payload, default=str))
    except Exception:
        return {"_unserializable": str(payload)}


class DbCoordinator:
    driver = "db"

    def __init__(
        self,
        *,
        instance_id: str,
        ttl: int,
        heartbeat_s: int,
        poll_ms: int,
        engine_getter: Optional[Callable[[], Any]] = None,
        should_prune: Optional[Callable[[], bool]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.instance_id = instance_id
        self.ttl = ttl
        self.heartbeat_s = heartbeat_s
        self.poll_s = max(0.1, int(poll_ms) / 1000.0)
        self._engine_getter = engine_getter or self._default_engine
        self._should_prune = should_prune or (lambda: False)
        # Test seam: replaces the database clock (naive UTC). Several
        # coordinators sharing one fake clock see the same "database time".
        self._now_fn = now_fn
        self._executor: Optional[ThreadPoolExecutor] = None
        self._subscribers: list[EventCallback] = []
        self._poll_task: Optional[asyncio.Task] = None
        self._last_event_id = 0
        self._last_prune = 0.0
        self._last_pool_refresh = 0.0
        self.pool_refreshes = 0
        self._started_at: Optional[datetime] = None
        self.started = False
        self.last_error: Optional[str] = None
        self.events_received = 0
        self.events_published = 0

    # ------------------------------------------------------------------
    # engine / schema
    # ------------------------------------------------------------------

    @staticmethod
    def _default_engine():
        from core.components.database.logic.db_service import db_instance

        if db_instance.engine is None:
            raise RuntimeError("database engine not initialised")
        return db_instance.engine

    def _engine(self):
        engine = self._engine_getter()
        if engine.dialect.name in ("mysql", "mariadb"):
            # READ COMMITTED: see module docstring (snapshot isolation → error 1020).
            return engine.execution_options(isolation_level="READ COMMITTED")
        return engine

    def _is_sqlite(self) -> bool:
        return self._engine_getter().dialect.name == "sqlite"

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run blocking coordinator SQL on this driver's own worker threads."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=_EXECUTOR_THREADS, thread_name_prefix="lyndrix-coord")
        return await asyncio.get_running_loop().run_in_executor(self._executor, functools.partial(fn, *args))

    def refresh_connections(self, reason: str = "") -> bool:
        """Start over with fresh pooled connections after a call timed out.

        A call that hung on a dead peer (DB VIP moved, half-open TCP) is
        abandoned by its caller, but the next one would take another pooled
        connection with the same dead peer and wait for the socket timeout
        again (``pool_pre_ping`` included) — one such round per pooled
        connection. Swapping the pool makes the next call connect anew
        (``connect_timeout`` 5 s). The old connections are only de-referenced
        (``close=False``): threads still inside a call finish on their own.

        Rate-limited to once per lease TTL so a merely slow database is not
        answered with a reconnect storm. Returns ``True`` when the pool was
        swapped. A no-op for SQLite (no sockets; an in-memory database would
        vanish with its pool).
        """
        try:
            engine = self._engine_getter()
        except Exception:
            return False
        if engine.dialect.name not in ("mysql", "mariadb"):
            return False
        now = time.monotonic()
        if self._last_pool_refresh and now - self._last_pool_refresh < max(self.ttl, 30):
            return False
        self._last_pool_refresh = now
        engine.dispose(close=False)
        self.pool_refreshes += 1
        log.warning("COORDINATOR: database connection pool refreshed (%s) — the next call opens new connections",
                    reason or "a call timed out")
        return True

    def ensure_schema(self) -> None:
        """Create the cluster_* tables once, cluster-wide.

        Two instances booting together both see "table missing" and both run
        CREATE — the loser got error 1050 and the coordinator never started.
        The bootstrap advisory lock serializes the DDL; 1050 is tolerated anyway
        for the window between checkfirst and CREATE.
        """
        from core.components.database.logic.db_service import Base, db_instance

        from ..models import ClusterLog

        tables = [ClusterInstance.__table__, ClusterLease.__table__, ClusterKV.__table__, ClusterEvent.__table__,
                  ClusterLog.__table__]
        engine = self._engine_getter()
        with db_instance.advisory_lock("lyndrix_bootstrap", timeout=60):
            try:
                Base.metadata.create_all(bind=engine, tables=tables, checkfirst=True)
            except OperationalError as exc:
                if _mysql_errno(exc) != 1050:  # 1050 = a peer created it first
                    raise
                log.info("COORDINATOR: cluster tables were created by another instance")
            self._add_missing_columns(engine)

    def _add_missing_columns(self, engine) -> None:
        """Additive upgrade of tables created by an older core (rolling update:
        the first new instance adds the columns while the old ones keep
        running — they neither read nor write them)."""
        existing_tables = set(inspect(engine).get_table_names())
        for table, columns in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {col["name"] for col in inspect(engine).get_columns(table)}
            for column, ddl in columns.items():
                if column in present:
                    continue
                log.warning("COORDINATOR: upgrading table %s — adding column '%s'", table, column)
                with engine.connect() as conn:
                    mysql = engine.dialect.name in ("mysql", "mariadb")
                    if mysql:
                        conn.execute(text(f"SET SESSION lock_wait_timeout = {_DDL_LOCK_WAIT_S}"))
                    try:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                        conn.commit()
                    except Exception as exc:
                        # 1060 / "duplicate column": a peer added it between our
                        # look and our ALTER — exactly what we wanted.
                        if _mysql_errno(exc) != 1060 and "duplicate column" not in str(exc).lower():
                            raise
                        conn.rollback()
                    finally:
                        if mysql:
                            conn.execute(text("SET SESSION lock_wait_timeout = DEFAULT"))

    def _db_now(self, conn) -> datetime:
        if self._now_fn is not None:
            return self._now_fn()
        if self._is_sqlite():
            raw = conn.execute(text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")).scalar()
            return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S.%f")
        value = conn.execute(text("SELECT UTC_TIMESTAMP(6)")).scalar()
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    def _retrying(self, fn: Callable[[], Any], *, attempts: int = 4) -> Any:
        delay = 0.05
        for attempt in range(1, attempts + 1):
            try:
                result = fn()
                self.last_error = None
                return result
            except OperationalError as exc:
                if _mysql_errno(exc) not in _RETRY_CODES or attempt == attempts:
                    self.last_error = str(exc.orig if getattr(exc, "orig", None) else exc)[:200]
                    raise
                time.sleep(delay + random.uniform(0, delay))
                delay = min(delay * 2, 0.5)
            except Exception as exc:
                self.last_error = str(exc)[:200]
                raise

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self.started:
            return
        await self._run(self.ensure_schema)
        self._last_event_id = await self._run(self._max_event_id)
        self._started_at = await self._run(self._now_sync)
        self.started = True
        self._poll_task = bus.create_tracked_task(self._poll_loop(), name="cluster:event_poll")
        log.info(
            "COORDINATOR: db driver started (instance %s, lease ttl %ss, poll %.0f ms, events from id %s)",
            self.instance_id, self.ttl, self.poll_s * 1000, self._last_event_id,
        )

    async def stop(self) -> None:
        self.started = False
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None
        try:
            await self._run(self._deregister_sync)
        except Exception as exc:  # pragma: no cover - best effort on shutdown
            log.debug("COORDINATOR: deregister failed: %s", exc)

    def _now_sync(self) -> datetime:
        with self._engine().connect() as conn:
            return self._db_now(conn)

    def _max_event_id(self) -> int:
        with self._engine().connect() as conn:
            value = conn.execute(text("SELECT MAX(id) FROM cluster_events")).scalar()
            return int(value or 0)

    def _deregister_sync(self) -> None:
        def _run():
            with self._engine().begin() as conn:
                now = self._db_now(conn)
                self._release_rows(conn, now, ClusterLease.holder == self.instance_id)
                conn.execute(delete(ClusterInstance).where(ClusterInstance.instance_id == self.instance_id))

        self._retrying(_run)

    # ------------------------------------------------------------------
    # instances
    # ------------------------------------------------------------------

    async def heartbeat(self, meta: dict) -> None:
        await self._run(self._heartbeat_sync, _json_safe(meta or {}))

    def _heartbeat_sync(self, meta: dict) -> None:
        from config import settings
        from core.api import __api_version__
        from version import __version__

        def _run():
            with self._engine().begin() as conn:
                now = self._db_now(conn)
                values = {
                    "hostname": socket.gethostname()[:255],
                    "roles": ",".join(settings.instance_roles)[:100],
                    "core_version": __version__,
                    "api_version": __api_version__,
                    "heartbeat_at": now,
                    "meta": meta,
                }
                result = conn.execute(
                    update(ClusterInstance).where(ClusterInstance.instance_id == self.instance_id).values(**values)
                )
                if result.rowcount:
                    return
                try:
                    conn.execute(insert(ClusterInstance).values(
                        instance_id=self.instance_id, started_at=self._started_at or now, **values,
                    ))
                except IntegrityError:
                    conn.execute(
                        update(ClusterInstance).where(ClusterInstance.instance_id == self.instance_id).values(**values)
                    )

        self._retrying(_run)

    async def instances(self) -> list[InstanceInfo]:
        return await self._run(self._instances_sync)

    def _instances_sync(self) -> list[InstanceInfo]:
        with self._engine().connect() as conn:
            now = self._db_now(conn)
            rows = conn.execute(select(ClusterInstance).order_by(ClusterInstance.started_at)).mappings().all()
        out: list[InstanceInfo] = []
        for row in rows:
            hb = row["heartbeat_at"]
            age = (now - hb).total_seconds() if isinstance(hb, datetime) else float("inf")
            out.append(InstanceInfo(
                instance_id=row["instance_id"],
                hostname=row["hostname"],
                roles=[r for r in (row["roles"] or "").split(",") if r],
                core_version=row["core_version"],
                api_version=row["api_version"],
                started_at=row["started_at"],
                heartbeat_at=hb,
                age_s=age,
                alive=age <= self.ttl,
                is_self=row["instance_id"] == self.instance_id,
                meta=dict(row["meta"] or {}),
            ))
        return out

    # ------------------------------------------------------------------
    # leases
    # ------------------------------------------------------------------

    async def try_acquire(self, name: str, ttl: Optional[float] = None) -> bool:
        return await self.acquire(name, ttl) is not None

    async def acquire(self, name: str, ttl: Optional[float] = None, *, fresh: bool = False) -> Optional[int]:
        """Acquire or renew ``name``; returns its fencing token, ``None`` when a
        peer holds it.

        ``fresh=True`` (the caller does not consider itself the holder) always
        starts a new acquisition with a new token — also when the row still
        names this instance, e.g. after the caller fenced itself locally or
        after a restart under a fixed instance id.
        """
        return await self._run(self._acquire_sync, name, float(ttl or self.ttl), bool(fresh))

    @staticmethod
    def _next_token(now: datetime):
        """SQL expression for the token of a new acquisition: one more than the
        row's last token, but never below "now" in microseconds on the database
        clock. The counter alone would restart at 1 when the row was deleted
        (core ≤ 1.11 releases by DELETE; the leader prunes idle rows); the
        clock alone could step back when the database fails over to a node
        with a slower clock. Together a token only grows."""
        floor = _epoch_micros(now)
        successor = func.coalesce(ClusterLease.token, 0) + 1
        return case((successor > floor, successor), else_=floor), floor

    def _acquire_sync(self, name: str, ttl: float, fresh: bool) -> Optional[int]:
        me = self.instance_id

        def _run() -> Optional[int]:
            with self._engine().begin() as conn:
                now = self._db_now(conn)
                expires = now + timedelta(seconds=ttl)
                next_token, floor = self._next_token(now)
                if fresh:
                    token_value, acquired_value = next_token, now
                else:
                    # An uninterrupted holding — the row is ours and has not
                    # expired — keeps its token and its acquisition time.
                    unbroken = and_(ClusterLease.holder == me, ClusterLease.expires_at >= now)
                    token_value = case(
                        (and_(unbroken, ClusterLease.token.is_not(None)), ClusterLease.token), else_=next_token)
                    acquired_value = case((unbroken, ClusterLease.acquired_at), else_=now)
                result = conn.execute(
                    update(ClusterLease)
                    .where(
                        ClusterLease.name == name,
                        or_(ClusterLease.holder == me, ClusterLease.holder.is_(None), ClusterLease.expires_at < now),
                    )
                    # ORDER MATTERS: MariaDB/MySQL evaluate SET left to right
                    # and later assignments see the values assigned before
                    # them. token and acquired_at depend on the OLD holder and
                    # expiry, so they come first (SQLAlchemy would otherwise
                    # emit table column order: holder first — and "the row is
                    # mine" would always be true).
                    .ordered_values(
                        (ClusterLease.token, token_value),
                        (ClusterLease.acquired_at, acquired_value),
                        (ClusterLease.holder, me),
                        (ClusterLease.renewed_at, now),
                        (ClusterLease.expires_at, expires),
                    )
                )
                if result.rowcount:
                    token = conn.execute(select(ClusterLease.token).where(ClusterLease.name == name)).scalar()
                    return int(token) if token is not None else floor
                existing = conn.execute(select(ClusterLease.holder).where(ClusterLease.name == name)).first()
                if existing is not None:
                    return None  # held by somebody else and not expired
                try:
                    conn.execute(insert(ClusterLease).values(
                        name=name, holder=me, acquired_at=now, renewed_at=now, expires_at=expires, token=floor,
                    ))
                    return floor
                except IntegrityError:
                    return None  # a peer inserted first

        return self._retrying(_run)

    async def release(self, name: str) -> bool:
        return await self._run(self._release_sync, name, self.instance_id)

    async def force_release(self, name: str) -> bool:
        return await self._run(self._release_sync, name, None)

    @staticmethod
    def _release_rows(conn, now: datetime, *criteria) -> int:
        """Mark matching held leases as free. The row stays (``holder = NULL``,
        expired) because it carries the token counter of that lease name."""
        return int(conn.execute(
            update(ClusterLease)
            .where(ClusterLease.holder.is_not(None), *criteria)
            .values(holder=None, expires_at=now, renewed_at=now)
        ).rowcount or 0)

    def _release_sync(self, name: str, holder: Optional[str]) -> bool:
        def _run() -> bool:
            with self._engine().begin() as conn:
                criteria = [ClusterLease.name == name]
                if holder is not None:
                    criteria.append(ClusterLease.holder == holder)
                return bool(self._release_rows(conn, self._db_now(conn), *criteria))

        return bool(self._retrying(_run))

    async def holder(self, name: str) -> Optional[LeaseInfo]:
        leases = await self._run(self._leases_sync, name)
        return leases[0] if leases else None

    async def leases(self) -> list[LeaseInfo]:
        return await self._run(self._leases_sync, None)

    def _leases_sync(self, name: Optional[str]) -> list[LeaseInfo]:
        with self._engine().connect() as conn:
            now = self._db_now(conn)
            # Released rows (holder NULL) only keep the token counter — they are
            # not leases anybody holds.
            stmt = select(ClusterLease).where(ClusterLease.holder.is_not(None)).order_by(ClusterLease.name)
            if name is not None:
                stmt = stmt.where(ClusterLease.name == name)
            rows = conn.execute(stmt).mappings().all()
        out = []
        for row in rows:
            expires = row["expires_at"]
            remaining = (expires - now).total_seconds() if isinstance(expires, datetime) else -1.0
            out.append(LeaseInfo(
                name=row["name"], holder=row["holder"], acquired_at=row["acquired_at"],
                expires_at=expires, remaining_s=remaining, expired=remaining < 0, token=row["token"],
            ))
        return out

    # ------------------------------------------------------------------
    # TTL key/value
    # ------------------------------------------------------------------

    async def kv_set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        await self._run(self._kv_set_sync, key, _json_safe({"v": value})["v"], ttl)

    def _kv_set_sync(self, key: str, value: Any, ttl: Optional[int]) -> None:
        def _run():
            with self._engine().begin() as conn:
                now = self._db_now(conn)
                expires = now + timedelta(seconds=int(ttl)) if ttl else None
                result = conn.execute(update(ClusterKV).where(ClusterKV.k == key).values(v=value, expires_at=expires))
                if result.rowcount:
                    return
                try:
                    conn.execute(insert(ClusterKV).values(k=key, v=value, expires_at=expires))
                except IntegrityError:
                    conn.execute(update(ClusterKV).where(ClusterKV.k == key).values(v=value, expires_at=expires))

        self._retrying(_run)

    async def kv_get(self, key: str) -> Any:
        return await self._run(self._kv_get_sync, key)

    def _kv_get_sync(self, key: str) -> Any:
        with self._engine().begin() as conn:
            now = self._db_now(conn)
            row = conn.execute(select(ClusterKV.v, ClusterKV.expires_at).where(ClusterKV.k == key)).first()
            if row is None:
                return None
            expires = row[1]
            if isinstance(expires, datetime) and expires <= now:
                conn.execute(delete(ClusterKV).where(ClusterKV.k == key))
                return None
            return row[0]

    async def kv_delete(self, key: str) -> bool:
        def _run() -> bool:
            with self._engine().begin() as conn:
                return bool(conn.execute(delete(ClusterKV).where(ClusterKV.k == key)).rowcount)

        return bool(await self._run(self._retrying, _run))

    async def kv_incr(self, key: str, ttl: Optional[int] = None) -> int:
        return await self._run(self._kv_incr_sync, key, ttl)

    def _kv_incr_sync(self, key: str, ttl: Optional[int]) -> int:
        def _run() -> int:
            with self._engine().begin() as conn:
                now = self._db_now(conn)
                stmt = select(ClusterKV.v, ClusterKV.expires_at).where(ClusterKV.k == key)
                if not self._is_sqlite():
                    stmt = stmt.with_for_update()
                row = conn.execute(stmt).first()
                expires_new = now + timedelta(seconds=int(ttl)) if ttl else None
                if row is None:
                    try:
                        conn.execute(insert(ClusterKV).values(k=key, v=1, expires_at=expires_new))
                        return 1
                    except IntegrityError:
                        row = conn.execute(stmt).first()
                        if row is None:  # pragma: no cover - vanished between statements
                            return 1
                current, expires = row[0], row[1]
                if isinstance(expires, datetime) and expires <= now:
                    value, expires = 1, expires_new
                else:
                    value, expires = int(current or 0) + 1, (expires or expires_new)
                conn.execute(update(ClusterKV).where(ClusterKV.k == key).values(v=value, expires_at=expires))
                return value

        return int(self._retrying(_run))

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    async def publish(self, topic: str, payload: dict) -> None:
        await self._run(self._publish_sync, topic, _json_safe(payload or {}))
        self.events_published += 1

    def _publish_sync(self, topic: str, payload: dict) -> None:
        def _run():
            with self._engine().begin() as conn:
                now = self._db_now(conn)
                conn.execute(insert(ClusterEvent).values(
                    topic=topic[:150], payload=payload, origin=self.instance_id, created_at=now,
                ))

        self._retrying(_run)

    def subscribe(self, callback: EventCallback) -> None:
        self._subscribers.append(callback)

    def _fetch_events_sync(self, after_id: int) -> list[dict]:
        with self._engine().connect() as conn:
            rows = conn.execute(
                select(ClusterEvent.id, ClusterEvent.topic, ClusterEvent.payload, ClusterEvent.origin)
                .where(ClusterEvent.id > after_id)
                .order_by(ClusterEvent.id)
                .limit(_EVENT_BATCH)
            ).mappings().all()
        return [dict(r) for r in rows]

    async def _poll_loop(self) -> None:
        failures = 0
        while self.started:
            try:
                rows = await self._run(self._fetch_events_sync, self._last_event_id)
                failures = 0
                for row in rows:
                    self._last_event_id = max(self._last_event_id, int(row["id"]))
                    if row["origin"] == self.instance_id:
                        continue
                    self.events_received += 1
                    self._dispatch(str(row["topic"]), dict(row["payload"] or {}), str(row["origin"]))
                if time.monotonic() - self._last_prune > _PRUNE_INTERVAL_S and self._should_prune():
                    self._last_prune = time.monotonic()
                    await self._run(self._prune_sync)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.last_error = str(exc)[:200]
                if failures in (1, 10, 100):
                    log.warning("COORDINATOR: event poll failed (%s×): %s", failures, exc)
                await asyncio.sleep(min(5.0, self.poll_s * failures))
                continue
            await asyncio.sleep(self.poll_s)

    def _dispatch(self, topic: str, payload: dict, origin: str) -> None:
        for callback in list(self._subscribers):
            try:
                result = callback(topic, payload, origin)
                if asyncio.iscoroutine(result):
                    bus.create_tracked_task(result, name=f"cluster:event:{topic}")
            except Exception:
                log.error("COORDINATOR: subscriber failed for '%s'", topic, exc_info=True)

    def _prune_sync(self) -> None:
        def _run():
            with self._engine().begin() as conn:
                now = self._db_now(conn)
                conn.execute(delete(ClusterEvent).where(ClusterEvent.created_at < now - _EVENT_RETENTION))
                dead_after = timedelta(seconds=max(3 * self.ttl, 120))
                conn.execute(delete(ClusterInstance).where(ClusterInstance.heartbeat_at < now - dead_after))
                conn.execute(delete(ClusterKV).where(ClusterKV.expires_at.is_not(None), ClusterKV.expires_at < now))
                conn.execute(delete(ClusterLease).where(
                    ClusterLease.holder.is_(None), ClusterLease.expires_at < now - _RELEASED_LEASE_RETENTION,
                ))

        self._retrying(_run)

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    async def db_now(self) -> datetime:
        return await self._run(self._now_sync)

    async def server_info(self) -> dict:
        return await self._run(self._server_info_sync)

    def _server_info_sync(self) -> dict:
        info: dict[str, Any] = {
            "driver": self.driver,
            "last_event_id": self._last_event_id,
            "events_received": self.events_received,
            "events_published": self.events_published,
            "last_error": self.last_error,
            "pool_refreshes": self.pool_refreshes,
        }
        try:
            with self._engine().connect() as conn:
                if self._is_sqlite():
                    info["dialect"] = "sqlite"
                    return info
                info["dialect"] = self._engine_getter().dialect.name
                info["version"] = str(conn.execute(text("SELECT @@version")).scalar())
                try:
                    info["wsrep_on"] = str(conn.execute(text("SELECT @@wsrep_on")).scalar()).upper() in ("1", "ON")
                except Exception:
                    info["wsrep_on"] = None
                try:
                    info["snapshot_isolation"] = str(
                        conn.execute(text("SELECT @@innodb_snapshot_isolation")).scalar()
                    ).upper() in ("1", "ON")
                except Exception:
                    info["snapshot_isolation"] = None
        except Exception as exc:
            info["error"] = str(exc)[:200]
        return info
