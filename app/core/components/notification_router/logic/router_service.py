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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect as sqlalchemy_inspect

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


class NotificationRouterService:
    def __init__(self) -> None:
        self.ctx = None
        self._schema_ready = False

    def bind(self, ctx) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Schema + persistence
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create plugin_notification_endpoints table if it does not exist."""
        if self._schema_ready or not db_instance.engine:
            return
        try:
            with db_instance.engine.begin() as connection:
                PluginNotificationEndpoint.__table__.create(bind=connection, checkfirst=True)
                # Sanity-check expected columns (additive migrations would go here).
                inspector = sqlalchemy_inspect(connection)
                _ = {c["name"] for c in inspector.get_columns(PluginNotificationEndpoint.__tablename__)}
            self._schema_ready = True
        except Exception as exc:
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

    async def sync_declared_endpoints(self) -> None:
        """Upsert current manifest declarations into the registry + DB.

        Also deletes DB rows whose declaration disappeared (plugin uninstalled
        or endpoint renamed). Run inside one transaction so cleanup is atomic.
        """
        if not db_instance.SessionLocal:
            return

        # Re-collect declared endpoints from the live module registry so the
        # registry stays in sync even if a plugin was hot-reloaded. This is
        # in-memory work, so it stays on the event loop.
        try:
            from core.components.plugins.logic.manager import module_manager
        except Exception:
            module_manager = None  # type: ignore[assignment]

        declared: dict[tuple[str, str], NotificationEndpoint] = {}
        if module_manager is not None:
            for entry in module_manager.registry.values():
                manifest = entry.get("manifest")
                if not manifest:
                    continue
                for ep in getattr(manifest, "notification_endpoints", []) or []:
                    declared[(manifest.id, ep.name)] = ep
                    endpoint_registry.upsert(manifest.id, ep)

        # Drop registry entries that no longer have a backing manifest.
        for plugin_id, ep in list(endpoint_registry.iter_all()):
            if (plugin_id, ep.name) not in declared:
                endpoint_registry.remove(plugin_id, ep.name)

        # Run the synchronous SQLAlchemy transaction off the event loop.
        def _sync() -> tuple[int, int]:
            self._ensure_schema()
            now = datetime.now(timezone.utc)
            with db_instance.SessionLocal() as session:
                existing = {
                    (row.plugin_id, row.endpoint_name): row
                    for row in session.query(PluginNotificationEndpoint).all()
                }

                # UPSERT every declared endpoint.
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
                    else:
                        row.declared_defaults = defaults_blob
                        row.updated_at = now
                    update_db_cache(
                        plugin_id,
                        endpoint_name,
                        is_active=row.is_active,
                        provider_bindings=row.provider_bindings,
                        required_permission_override=row.required_permission_override,
                    )

                # Anything left in ``existing`` is stale → delete.
                stale_count = len(existing)
                for (plugin_id, endpoint_name), row in existing.items():
                    session.delete(row)
                    remove_from_db_cache(plugin_id, endpoint_name)

                session.commit()
            return len(declared), stale_count

        try:
            n_declared, n_stale = await asyncio.to_thread(_sync)
            log.info(
                f"ROUTER: Synced {n_declared} declared endpoint(s); pruned {n_stale} stale row(s)."
            )
        except Exception as exc:
            log.error(f"ROUTER: sync_declared_endpoints failed: {exc}")

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

        # Run the synchronous SQLAlchemy write off the event loop.
        def _update() -> None:
            self._ensure_schema()
            now = datetime.now(timezone.utc)
            with db_instance.SessionLocal() as session:
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
                notification_service.broadcast_toast(
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
