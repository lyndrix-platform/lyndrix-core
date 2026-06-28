"""
InternalNotificationAdapter — the built-in "system" provider.

Bridges OutboundMessage objects into the existing Lyndrix UI notification
bell (notification_service).  It is the only adapter that is always present
regardless of which external plugins are installed.

Special behaviour
-----------------
- ``metadata["action"] == "clear"`` with ``metadata["notification_id"]``
  removes an existing notification from the bell by ID (replaces the old
  ``{"action": "clear", "id": "..."}`` pattern in system:notify calls).
- ``metadata["toast"]`` (default True) controls whether the floating toast
  is shown in addition to the bell entry.
- ``metadata["persist"]`` (default True) controls whether the entry is saved
  to the notification history.
"""
from __future__ import annotations

import logging

from core.components.messaging.logic.adapter import GatewayAdapter, GatewayCapability
from core.components.messaging.model.schemas import OutboundMessage

log = logging.getLogger("Core:InternalAdapter")


class InternalNotificationAdapter(GatewayAdapter):
    """Routes OutboundMessage to the Lyndrix UI notification bell."""

    provider_id   = "system"
    display_name  = "Lyndrix Internal Notifications"
    capabilities  = GatewayCapability.TEXT | GatewayCapability.RICH_MEDIA

    async def send(self, message: OutboundMessage) -> str | None:
        # Lazy import to avoid circular-import at module load time
        from core.components.notifications.logic.notification_service import notification_service

        meta = message.metadata or {}

        # Handle clear action
        if meta.get("action") == "clear":
            notif_id = meta.get("notification_id")
            if notif_id:
                notification_service.remove_notification(str(notif_id))
                log.debug("InternalAdapter: cleared notification '%s'.", notif_id)
            return "cleared"

        payload: dict = {
            "title":          message.title,
            "message":        message.body,
            "type":           message.severity.to_legacy(),
            "toast":          bool(meta.get("toast", True)),
            "persist":        bool(meta.get("persist", True)),
            # Never re-emit outbound from the internal adapter — would create a loop
            "emit_outbound":  False,
        }
        notif_id = meta.get("notification_id")
        if notif_id:
            payload["id"] = str(notif_id)

        await notification_service.handle_system_notify(payload)
        log.debug("InternalAdapter: delivered '%s' to notification bell.", message.title)
        return "delivered"
