"""Notification Router — CORE component.

Owns the routing pipeline between plugin code and the existing in-app
notification service + messaging gateway.

Boot integration:
  - subscribed at setup() so the bus topic is wired before any plugin loads.
  - on ``db:connected`` hydrates the precedence cache from the DB and runs
    the discovery sync (UPSERT current manifests, prune stale rows).
"""
from __future__ import annotations

from core.i18n import reload_core_locales
from core.components.database.logic.db_service import db_instance
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

# Ensure our locale file is loaded even if i18n was imported before the file existed.
reload_core_locales()

from .logic.router_service import notification_router


manifest = ModuleManifest(
    id="lyndrix.core.notification_router",
    name="Notification Router",
    version="1.0.0",
    description="Routes plugin-declared notification endpoints to internal UI and external providers.",
    author="Lyndrix",
    icon="campaign",
    type="CORE",
    permissions=ModulePermissions(
        subscribe=["db:connected", "notification:routed", "ui:needs_refresh"],
        emit=["messaging:outbound", "ui:needs_refresh"],
    ),
)


def setup(ctx):
    notification_router.bind(ctx)

    async def _hydrate_and_sync():
        await notification_router.hydrate_from_db()
        await notification_router.sync_declared_endpoints()

    if db_instance.is_connected:
        ctx.create_task(_hydrate_and_sync(), name="notification_router:initial_sync")

    @ctx.subscribe("db:connected")
    async def _on_db(_payload):
        await notification_router.hydrate_from_db()
        await notification_router.sync_declared_endpoints()

    @ctx.subscribe("notification:routed")
    async def _on_envelope(payload):
        await notification_router.handle_envelope(payload or {})

    @ctx.subscribe("ui:needs_refresh")
    async def _on_ui_refresh(_payload):
        if db_instance.is_connected:
            await notification_router.sync_declared_endpoints()

    ctx.log.info("Notification Router: setup complete")
