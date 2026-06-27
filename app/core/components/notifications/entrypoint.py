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
        "subscribe": ["system:notify", "user:notify"],
        "emit": ["notification:outbound"]
    }
)

def setup(ctx):
    ctx.log.info("Notification Manager initialisiert...")
    notification_service.set_context(ctx)
    ctx.subscribe("system:notify")(notification_service.handle_system_notify)
    ctx.subscribe("user:notify")(notification_service.handle_user_notify)