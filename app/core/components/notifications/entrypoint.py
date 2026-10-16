from core.components.plugins.logic.models import ModuleManifest
from .logic.notification_service import notification_service

manifest = ModuleManifest(
    id="lyndrix.core.notifications",
    name="Notification Manager",
    version="1.0.0",
    description="Zentrale Komponente für systemweite Benachrichtigungen und Plugin-Hooks.",
    author="Lyndrix",
    icon="notifications_active",
    type="CORE",
    permissions={
        "subscribe": ["system:notify", "user:notify", "db:connected"],
        "emit": ["notification:outbound"]
    }
)

def setup(ctx):
    ctx.log.info("Notification Manager initialisiert...")
    notification_service.set_context(ctx)
    ctx.subscribe("system:notify")(notification_service.handle_system_notify)
    ctx.subscribe("user:notify")(notification_service.handle_user_notify)

    # Shared store (core 1.8): switch from the per-instance file to the database
    # as soon as it is connected (db:connected usually fires before this core
    # component is set up, so check right away as well).
    import asyncio

    from core.components.database.logic.db_service import db_instance

    async def _enable_shared_store(payload=None):
        if db_instance.is_connected and not notification_service.db_enabled:
            await asyncio.to_thread(notification_service.enable_db)

    ctx.subscribe("db:connected")(_enable_shared_store)
    if db_instance.is_connected:
        ctx.create_task(_enable_shared_store(), name="notifications:shared_store")