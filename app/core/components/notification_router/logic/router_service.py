"""Notification router service — consumes ``notification:routed`` envelopes.

Pipeline per envelope:
  1. Validate envelope against the in-memory endpoint registry. Unknown
     ``(source_plugin_id, endpoint_name)`` → WARNING log + drop.
  2. Resolve active/provider via env > DB > default precedence.
  3. If inactive → silent drop.
  4. Apply internal effects through ``notification_service`` (toast/persist).
     Note: we persist via ``notification_service.persist(..., emit_outbound=False)``
     so the internal bell entry is written without re-emitting
     ``notification:outbound``.
  5. Dispatch externally through ``messaging_gateway`` when a valid provider
     is resolved and registered.
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect as sqlalchemy_inspect, text

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from core.components.plugins.logic.models import (
    NotificationEndpoint,
    PluginNotificationEndpoint,
)
from core.components.messaging.logic.gateway import messaging_gateway
from core.components.messaging.model.schemas import OutboundMessage

from ..models import NotificationEnvelope, ResolvedState
from .endpoint_registry import endpoint_registry
from .precedence import (
    remove_from_db_cache,
    remove_plugin_from_db_cache,
    resolve_endpoint_state,
    update_db_cache,
)

log = get_logger("Core:NotificationRouter")


_UNSET = object()


def _humanize(endpoint_name: str) -> str:
    return endpoint_name.replace("_", " ").title()


def _payload_message(envelope: NotificationEnvelope) -> str:
    if envelope.body is not None:
        return envelope.body
    msg = envelope.payload.get("message")
    return str(msg) if msg is not None else ""


def _payload_title(envelope: NotificationEnvelope) -> str:
    if envelope.title is not None:
        return envelope.title
    title = envelope.payload.get("title")
    return str(title) if title is not None else _humanize(envelope.endpoint_name)


# --- Discovery sync tuning (audit finding BG-03) -------------------------
# A burst of lifecycle events (install = files_changed + state_changed, an
# "upgrade all" = k of those) collapses into one sync after this quiet period.
_SYNC_DEBOUNCE_SECONDS = 2.0
# Cross-instance serialization. Short on purpose: a sync that cannot get the
# lock is SKIPPED and retried later — it never runs unlocked and never parks a
# worker thread for long.
_SYNC_LOCK_NAME = "lyndrix_notification_sync"
_SYNC_LOCK_TIMEOUT_SECONDS = 3
# Row lock wait inside the sync / binding transaction (server default: 50 s).
_LOCK_WAIT_SECONDS = 5
# After a skipped ("busy") sync: wait this long (jittered) before the next try,
# and give up after this many consecutive skips — the next trigger starts over.
_BUSY_RETRY_SECONDS = (3.0, 8.0)
_BUSY_MAX_ROUNDS = 6
# After that many busy rounds the runner ends (callers waiting for it must
# not hang), but the sync is still owed: it is asked for again after this
# delay, doubling up to the maximum while the lock stays busy.
_BUSY_DEFERRED_RETRY_SECONDS = 60.0
_BUSY_DEFERRED_RETRY_MAX_SECONDS = 600.0


class _SyncBusy(Exception):
    """The cross-instance sync lock is held by a peer — skip this round."""


def _has_operator_config(row: PluginNotificationEndpoint) -> bool:
    """True when an operator configured this endpoint (anything not NULL)."""
    return (
        row.is_active is not None
        or row.provider_bindings is not None
        or row.required_permission_override is not None
    )


class NotificationRouterService:
    def __init__(self) -> None:
        self.ctx = None
        self._schema_ready = False
        # Coalescing state — only touched on the event loop.
        self._sync_dirty = False
        self._sync_task: asyncio.Task | None = None
        self._deferred_retry: asyncio.TimerHandle | None = None
        self._deferred_retries = 0     # consecutive give-ups (drives the delay)
        # Counters for diagnostics and tests.
        self.sync_runs = 0
        self.sync_skipped_busy = 0

    def bind(self, ctx) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Schema + persistence
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the endpoint table, and additively add any columns a table
        created by an older core is missing.

        ``create(checkfirst=True)`` only creates the table when absent; it never
        touches an existing table. A deployment whose ``plugin_notification_endpoints``
        table predates a model column (e.g. ``provider_bindings`` from the
        multi-provider routing work) would therefore keep failing every query
        with ``Unknown column``. We reconcile the live columns against the model
        and ``ALTER TABLE ADD COLUMN`` the gaps. Every added column is nullable
        (or Python-side defaulted), so backfill is unnecessary.

        Runs under the cluster-wide bootstrap lock: instances booting together
        used to race the CREATE/ALTER (error 1050/1060). A race that is lost
        anyway means a peer created the same schema — treated as done.
        """
        if self._schema_ready or not db_instance.engine:
            return
        from core.components.database.logic.tx import is_ddl_race

        try:
            table = PluginNotificationEndpoint.__table__
            with db_instance.advisory_lock("lyndrix_bootstrap"):
                with db_instance.engine.begin() as connection:
                    table.create(bind=connection, checkfirst=True)
                    inspector = sqlalchemy_inspect(connection)
                    existing = {c["name"] for c in inspector.get_columns(table.name)}
                    dialect = connection.dialect
                    for column in table.columns:
                        if column.name in existing:
                            continue
                        col_type = column.type.compile(dialect=dialect)
                        connection.execute(
                            text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type} NULL')
                        )
                        log.warning(
                            "ROUTER: added missing column '%s.%s' (%s) — migrated from an older schema.",
                            table.name, column.name, col_type,
                        )
            self._schema_ready = True
        except Exception as exc:
            if is_ddl_race(exc):
                log.info("ROUTER: endpoint schema was created by a peer instance.")
                self._schema_ready = True
                return
            log.error(f"ROUTER: Failed to ensure endpoint schema: {exc}")

    async def hydrate_from_db(self) -> None:
        """Load all stored endpoint bindings into the in-memory precedence cache."""
        if not db_instance.SessionLocal:
            return

        # Run the synchronous SQLAlchemy work off the event loop.
        def _hydrate() -> int:
            self._ensure_schema()
            with db_instance.SessionLocal() as session:
                rows = session.query(PluginNotificationEndpoint).all()
                for row in rows:
                    update_db_cache(
                        row.plugin_id,
                        row.endpoint_name,
                        is_active=row.is_active,
                        provider_bindings=row.provider_bindings,
                        required_permission_override=row.required_permission_override,
                    )
                return len(rows)

        try:
            count = await asyncio.to_thread(_hydrate)
            log.info(f"ROUTER: Hydrated {count} endpoint binding(s) from DB.")
        except Exception as exc:
            log.error(f"ROUTER: hydrate_from_db failed: {exc}")

    # ------------------------------------------------------------------
    # Discovery sync — coalesced
    # ------------------------------------------------------------------

    def request_sync(self) -> None:
        """Ask for a discovery sync soon (fire-and-forget, debounced).

        Event handlers call this. However many requests arrive, at most ONE sync
        runs at a time; requests during a run set a dirty flag and are served by
        one follow-up pass. Before, every trigger spawned its own worker-thread
        sync and N instances × several events per install queued up behind each
        other's locks.
        """
        self._sync_dirty = True
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.get_running_loop().create_task(
                self._sync_runner(_SYNC_DEBOUNCE_SECONDS), name="notification_router:sync"
            )

    async def sync_declared_endpoints(self) -> None:
        """Sync now and wait until a pass that covers this call has finished.

        Joins the running sync when there is one (single-flight) instead of
        starting a second transaction next to it.
        """
        if not db_instance.SessionLocal:
            return
        self._sync_dirty = True
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.get_running_loop().create_task(
                self._sync_runner(0.0), name="notification_router:sync"
            )
        # shield: a cancelled caller must not cancel the shared runner.
        await asyncio.shield(self._sync_task)

    async def _sync_runner(self, debounce: float) -> None:
        busy_rounds = 0
        while self._sync_dirty:
            if debounce > 0:
                await asyncio.sleep(debounce)
            self._sync_dirty = False
            outcome = await self._sync_pass()
            if outcome != "busy":
                busy_rounds = 0
                self._deferred_retries = 0
                continue
            # A peer holds the sync lock. Do not run unlocked (that produced the
            # 1020/1205/1213 storms) — stay dirty and look again a bit later.
            busy_rounds += 1
            self.sync_skipped_busy += 1
            if busy_rounds >= _BUSY_MAX_ROUNDS:
                # Giving up for good left this instance's endpoints unsynced
                # until some later plugin lifecycle event — possibly never.
                delay = self._defer_sync()
                log.warning(
                    "ROUTER: endpoint sync skipped %d times (sync lock busy) — "
                    "trying again in %.0f s.", busy_rounds, delay,
                )
                return
            self._sync_dirty = True
            await asyncio.sleep(random.uniform(*_BUSY_RETRY_SECONDS))

    def _defer_sync(self) -> float:
        """Ask for the owed sync again later; returns the delay in seconds."""
        delay = min(_BUSY_DEFERRED_RETRY_SECONDS * (2 ** self._deferred_retries), _BUSY_DEFERRED_RETRY_MAX_SECONDS)
        self._deferred_retries += 1
        if self._deferred_retry is not None:
            self._deferred_retry.cancel()
        self._deferred_retry = asyncio.get_running_loop().call_later(delay, self._deferred_sync)
        return delay

    def _deferred_sync(self) -> None:
        self._deferred_retry = None
        try:
            self.request_sync()
        except Exception as exc:  # pragma: no cover - loop shutting down
            log.debug(f"ROUTER: deferred endpoint sync could not be scheduled: {exc}")

    def _collect_declared(self) -> tuple[dict[tuple[str, str], NotificationEndpoint], set[str]]:
        """Refresh the in-memory endpoint registry from the loaded manifests.

        Returns the declared endpoints and the ids of every module loaded on
        THIS instance. In-memory work — stays on the event loop.
        """
        try:
            from core.components.plugins.logic.manager import module_manager
        except Exception:
            module_manager = None  # type: ignore[assignment]

        declared: dict[tuple[str, str], NotificationEndpoint] = {}
        loaded: set[str] = set()
        if module_manager is not None:
            for entry in list(module_manager.registry.values()):
                manifest = entry.get("manifest")
                if not manifest:
                    continue
                loaded.add(manifest.id)
                for ep in getattr(manifest, "notification_endpoints", []) or []:
                    declared[(manifest.id, ep.name)] = ep
                    endpoint_registry.upsert(manifest.id, ep)

        # Drop registry entries that no longer have a backing manifest.
        for plugin_id, ep in list(endpoint_registry.iter_all()):
            if (plugin_id, ep.name) not in declared:
                endpoint_registry.remove(plugin_id, ep.name)
        return declared, loaded

    async def _sync_pass(self) -> str:
        """One sync pass. Returns ``"ok"``, ``"busy"`` (lock held by a peer) or ``"error"``."""
        if not db_instance.SessionLocal:
            return "error"
        declared, loaded = self._collect_declared()
        self.sync_runs += 1
        try:
            n_declared, n_written, n_pruned = await asyncio.to_thread(self._sync, declared, loaded)
        except _SyncBusy:
            log.debug("ROUTER: endpoint sync lock is busy — retrying later.")
            return "busy"
        except Exception as exc:
            log.error(f"ROUTER: sync_declared_endpoints failed: {exc}")
            return "error"
        if n_written or n_pruned:
            log.info(
                f"ROUTER: Synced {n_declared} declared endpoint(s); "
                f"wrote {n_written} row(s), pruned {n_pruned} stale row(s)."
            )
        else:
            log.debug(f"ROUTER: {n_declared} declared endpoint(s) already in sync.")
        return "ok"

    def _sync(
        self,
        declared: dict[tuple[str, str], NotificationEndpoint],
        loaded: set[str],
    ) -> tuple[int, int, int]:
        """Blocking part of a sync pass (worker thread).

        Serialized across instances with a short advisory lock. On MySQL/MariaDB
        a lock that cannot be obtained raises :class:`_SyncBusy` (caller retries
        later); only dialects without ``GET_LOCK`` (SQLite tests) run without it.
        Transient 1020/1205/1213 are retried with a fresh session.
        """
        from core.components.database.logic.tx import run_with_retry

        self._ensure_schema()
        with db_instance.advisory_lock(
            _SYNC_LOCK_NAME, timeout=_SYNC_LOCK_TIMEOUT_SECONDS, warn=False
        ) as locked:
            if not locked and self._lock_is_mandatory():
                raise _SyncBusy()
            return run_with_retry(
                lambda: self._sync_once(declared, loaded), attempts=5, retry_integrity=True
            )

    @staticmethod
    def _lock_is_mandatory() -> bool:
        """The advisory lock exists only on MySQL/MariaDB — there it is required."""
        from core.components.database.logic.tx import is_mysql

        return is_mysql()

    def _sync_once(
        self,
        declared: dict[tuple[str, str], NotificationEndpoint],
        loaded: set[str],
    ) -> tuple[int, int, int]:
        """One transaction: insert new endpoints, update CHANGED declarations,
        prune rows of uninstalled plugins. Writes nothing (takes no row lock,
        commits nothing) when the DB already matches the manifests — the common
        case, which used to rewrite and X-lock every row on every trigger."""
        from core.components.database.logic.tx import write_session

        now = datetime.now(timezone.utc)
        written = pruned = 0
        cache_updates: list[PluginNotificationEndpoint] = []
        cache_removals: list[tuple[str, str]] = []
        with write_session(_LOCK_WAIT_SECONDS) as session:
            existing = {
                (row.plugin_id, row.endpoint_name): row
                for row in session.query(PluginNotificationEndpoint).all()
            }

            for (plugin_id, endpoint_name), ep in declared.items():
                defaults_blob = json.dumps(ep.model_dump())
                row = existing.pop((plugin_id, endpoint_name), None)
                if row is None:
                    row = PluginNotificationEndpoint(
                        plugin_id=plugin_id,
                        endpoint_name=endpoint_name,
                        is_active=None,
                        provider_bindings=None,
                        required_permission_override=None,
                        declared_defaults=defaults_blob,
                        discovered_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    written += 1
                elif row.declared_defaults != defaults_blob:
                    row.declared_defaults = defaults_blob
                    row.updated_at = now
                    written += 1
                cache_updates.append(row)

            # Rows without a declaration on THIS instance. "Not in my registry"
            # is not "uninstalled": the plugin may be mid-reload, failed to load
            # here, or not rolled out to this instance yet — deleting then wiped
            # the operator's bindings cluster-wide.
            if existing:
                installed = self._installed_plugin_ids(session)
                for (plugin_id, endpoint_name), row in existing.items():
                    if not self._may_prune(row, plugin_id, loaded, installed):
                        continue
                    session.delete(row)
                    cache_removals.append((plugin_id, endpoint_name))
                    pruned += 1

            if written or pruned:
                session.commit()

            # Publish to the in-memory cache only after the transaction stands
            # (a rolled-back/retried attempt must not leave phantom entries).
            for row in cache_updates:
                update_db_cache(
                    row.plugin_id,
                    row.endpoint_name,
                    is_active=row.is_active,
                    provider_bindings=row.provider_bindings,
                    required_permission_override=row.required_permission_override,
                )
            for plugin_id, endpoint_name in cache_removals:
                remove_from_db_cache(plugin_id, endpoint_name)
        return len(declared), written, pruned

    @staticmethod
    def _installed_plugin_ids(session) -> set[str] | None:
        """Ids with a ``plugin_states`` row = installed somewhere in the cluster.

        ``None`` when that cannot be determined (table missing, query failed) —
        the caller then prunes nothing.
        """
        from core.components.plugins.logic.models import PluginState

        try:
            return {mid for (mid,) in session.query(PluginState.module_id).all()}
        except Exception as exc:
            log.debug(f"ROUTER: plugin_states not readable ({exc}) — skipping stale-row pruning.")
            return None

    @staticmethod
    def _may_prune(
        row: PluginNotificationEndpoint,
        plugin_id: str,
        loaded: set[str],
        installed: set[str] | None,
    ) -> bool:
        """Decide whether an undeclared endpoint row may be deleted.

        * Plugin really uninstalled (no ``plugin_states`` row and not loaded
          here — core modules have no such row but are always loaded): delete
          all of its rows.
        * Plugin loaded here but the endpoint is gone from its manifest
          (renamed/removed): delete only a row WITHOUT operator config.
        * Anything else (installed but not loaded here, state unknown): keep.
        """
        if installed is None:
            return False
        if plugin_id not in installed and plugin_id not in loaded:
            return True
        if plugin_id in loaded:
            # TODO(agent): rows that keep operator config for an endpoint a plugin
            # no longer declares stay as orphans; offer an explicit cleanup
            # (settings action) instead of guessing here.
            return not _has_operator_config(row)
        return False

    async def update_binding(
        self,
        plugin_id: str,
        endpoint_name: str,
        *,
        active: bool | None = None,
        providers: Any = _UNSET,
        required_permission: Any = _UNSET,
    ) -> ResolvedState:
        """Persist a binding change and refresh the precedence cache.

        ``active=None`` leaves active untouched. ``providers``: ``_UNSET`` =
        untouched, ``None``/``[]`` = clear (no external delivery), or a list of
        provider ids (fan-out). ``required_permission``: ``_UNSET`` = untouched,
        ``None`` = reset to the manifest default (row NULL), ``""`` = explicit
        no-restriction, or a permission id.
        """
        if endpoint_registry.get(plugin_id, endpoint_name) is None:
            raise ValueError(f"Unknown endpoint: {plugin_id}/{endpoint_name}")
        if not db_instance.SessionLocal:
            raise RuntimeError("Database is not available")

        from core.components.database.logic.tx import run_with_retry, write_session

        # One attempt = one fresh READ COMMITTED session. An operator's save that
        # collides with a running sync (1020/1205/1213) or with a concurrent
        # first insert of the row is retried instead of surfacing as HTTP 500.
        def _update_once() -> None:
            now = datetime.now(timezone.utc)
            with write_session(_LOCK_WAIT_SECONDS) as session:
                row = (
                    session.query(PluginNotificationEndpoint)
                    .filter(
                        PluginNotificationEndpoint.plugin_id == plugin_id,
                        PluginNotificationEndpoint.endpoint_name == endpoint_name,
                    )
                    .first()
                )
                if row is None:
                    row = PluginNotificationEndpoint(
                        plugin_id=plugin_id,
                        endpoint_name=endpoint_name,
                        is_active=None,
                        provider_bindings=None,
                        required_permission_override=None,
                        discovered_at=now,
                        updated_at=now,
                    )
                    session.add(row)

                if active is not None:
                    row.is_active = bool(active)
                if providers is not _UNSET:
                    row.provider_bindings = list(providers) if providers else None
                if required_permission is not _UNSET:
                    # Tri-state: None resets to manifest default (NULL row).
                    row.required_permission_override = required_permission
                row.updated_at = now
                session.commit()

                update_db_cache(
                    plugin_id,
                    endpoint_name,
                    is_active=row.is_active,
                    provider_bindings=row.provider_bindings,
                    required_permission_override=row.required_permission_override,
                )

        # Run the synchronous SQLAlchemy write off the event loop.
        def _update() -> None:
            self._ensure_schema()
            run_with_retry(_update_once, attempts=5, retry_integrity=True)

        try:
            await asyncio.to_thread(_update)
        except Exception as exc:
            log.error(f"ROUTER: update_binding failed for {plugin_id}/{endpoint_name}: {exc}")
            raise

        return resolve_endpoint_state(plugin_id, endpoint_name)

    def forget_plugin(self, plugin_id: str) -> None:
        """Drop registry + cache for an uninstalled plugin (no DB delete here)."""
        endpoint_registry.remove_plugin(plugin_id)
        remove_plugin_from_db_cache(plugin_id)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def handle_envelope(self, raw: dict) -> None:
        try:
            envelope = NotificationEnvelope(**raw)
        except Exception as exc:
            log.warning(f"ROUTER: dropping malformed envelope: {exc}")
            return

        decl = endpoint_registry.get(envelope.source_plugin_id, envelope.endpoint_name)
        if decl is None:
            log.warning(
                "ROUTER: dropping envelope for unknown endpoint %s/%s — declare it in the manifest.",
                envelope.source_plugin_id,
                envelope.endpoint_name,
            )
            return

        state = resolve_endpoint_state(envelope.source_plugin_id, envelope.endpoint_name)
        if not state.active:
            return

        await self._apply_internal(envelope, decl, state)
        await self._apply_external(envelope, decl, state)

    async def _apply_internal(
        self,
        envelope: NotificationEnvelope,
        decl: NotificationEndpoint,
        state: ResolvedState,
    ) -> None:
        # Clear semantics: remove a sticky notification by id and skip everything else.
        if envelope.clear and envelope.notification_id:
            try:
                from core.components.notifications.logic.notification_service import notification_service
                notification_service.remove_notification(envelope.notification_id)
            except Exception as exc:
                log.error(f"ROUTER: failed to clear notification: {exc}")
            return

        if not (decl.internal_toast or decl.internal_persist):
            return

        try:
            from core.components.notifications.logic.notification_service import notification_service
        except Exception as exc:
            log.error(f"ROUTER: notification_service unavailable: {exc}")
            return

        message = _payload_message(envelope)
        title = _payload_title(envelope)
        legacy_type = envelope.severity.to_legacy()

        if decl.internal_persist:
            try:
                notification_service.persist(
                    {
                        "id": envelope.notification_id,
                        "title": title,
                        "message": message,
                        "type": legacy_type,
                        "toast": False,
                        "persist": True,
                        # Routing v2 metadata: audience gate + mute key.
                        "required_permission": state.required_permission,
                        "source_plugin_id": envelope.source_plugin_id,
                        "endpoint_name": envelope.endpoint_name,
                    },
                    broadcast=False,
                    emit_outbound=False,
                )
            except Exception as exc:
                log.error(f"ROUTER: notification persist failed: {exc}")

        if decl.internal_toast:
            try:
                # Async variant: permission lookups for the audience gate run
                # off the event loop.
                await notification_service.broadcast_toast_async(
                    message, legacy_type,
                    required_permission=state.required_permission,
                )
            except Exception as exc:
                log.error(f"ROUTER: toast broadcast failed: {exc}")

    async def _apply_external(
        self,
        envelope: NotificationEnvelope,
        decl: NotificationEndpoint,
        state: ResolvedState,
    ) -> None:
        if not state.providers:
            return

        # Fan-out: one explicit OutboundMessage per configured provider —
        # deliberately NOT the gateway broadcast path (target_provider=None),
        # which would implicitly include every future adapter. asyncio.gather
        # keeps a slow adapter from delaying the others (each is still bounded
        # by its own send_timeout).
        registered = {adapter.provider_id for adapter in messaging_gateway.list_adapters()}
        targets = [p for p in state.providers if p in registered]
        for missing in [p for p in state.providers if p not in registered]:
            log.warning(
                "ROUTER: provider '%s' not registered (endpoint %s/%s) — skipping.",
                missing, envelope.source_plugin_id, envelope.endpoint_name,
            )
        if not targets:
            return

        async def _send(pid: str):
            outbound = OutboundMessage(
                title=_payload_title(envelope),
                body=_payload_message(envelope),
                severity=envelope.severity,
                source_plugin_id=envelope.source_plugin_id,
                target_provider=pid,
                correlation_id=envelope.correlation_id,
                metadata={
                    "endpoint_name":   envelope.endpoint_name,
                    "notification_id": envelope.notification_id,
                    "lyndrix_payload": envelope.payload,
                },
            )
            return pid, await messaging_gateway.dispatch(outbound)

        gathered = await asyncio.gather(*(_send(p) for p in targets), return_exceptions=True)
        for item in gathered:
            if isinstance(item, BaseException):
                log.error(
                    "ROUTER: external dispatch error for %s/%s: %s",
                    envelope.source_plugin_id, envelope.endpoint_name, item,
                )
                continue
            pid, results = item
            for rpid, result in results.items():
                if result.success:
                    log.debug(
                        "ROUTER: %s/%s → %s delivered (msg_id=%s).",
                        envelope.source_plugin_id, envelope.endpoint_name,
                        rpid, result.message_id,
                    )
                elif result.queued_for_retry:
                    log.info(
                        "ROUTER: %s/%s → %s queued for retry.",
                        envelope.source_plugin_id, envelope.endpoint_name, rpid,
                    )
                else:
                    log.error(
                        "ROUTER: %s/%s → %s delivery failed: %s",
                        envelope.source_plugin_id, envelope.endpoint_name,
                        rpid, result.error,
                    )


notification_router = NotificationRouterService()
