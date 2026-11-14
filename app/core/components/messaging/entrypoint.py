"""
Messaging Gateway — CORE component entrypoint.

Boot sequence wiring:
  db:connected        → init DB table + reload pending actions + start retry workers
  messaging:outbound  → route OutboundMessage through all/targeted adapters

The built-in "system" provider (InternalNotificationAdapter) is registered
here so it is always available regardless of which external plugins are
installed.

Note on notification:outbound
------------------------------
The gateway no longer subscribes to ``notification:outbound`` to avoid
duplicate delivery: the Discord adapter keeps its own legacy subscription
for backward compatibility with code that has not yet been migrated to
``messaging:outbound``.  The InternalNotificationAdapter is reached via
``messaging:outbound`` with ``target_provider="system"``.
"""
import asyncio

from core.api import ModuleManifest
from core.components.messaging.logic.gateway import messaging_gateway
from core.components.messaging.logic.internal_adapter import InternalNotificationAdapter
from core.components.messaging.model.schemas import OutboundMessage

manifest = ModuleManifest(
    id="lyndrix.core.messaging_gateway",
    name="Messaging Gateway",
    version="1.1.0",
    description=(
        "Centralized two-way messaging registry. "
        "External plugins register adapters (Discord, Telegram…) at runtime."
    ),
    author="Lyndrix",
    icon="hub",
    type="CORE",
    permissions={
        "subscribe": [
            "db:connected",
            "messaging:outbound",
        ],
        "emit": ["messaging:action_response"],
    },
)


_EXPIRE_INTERVAL_SECONDS = 300
_STREAM_CLEANUP_INTERVAL_SECONDS = 600


def setup(ctx):
    ctx.log.info("Messaging Gateway: initialising…")

    # Pull configurable limits from settings before any adapter is registered.
    try:
        from config import settings
        messaging_gateway.configure(
            max_retries=settings.LYNDRIX_GATEWAY_MAX_RETRY_ATTEMPTS
        )
    except Exception as exc:
        ctx.log.warning("Messaging Gateway: failed to apply retry config: %s", exc)

    # Always-present system provider — must be registered before plugins boot.
    messaging_gateway.register(InternalNotificationAdapter())
    ctx.log.info("Messaging Gateway: registered built-in 'system' adapter.")

    @ctx.subscribe("db:connected")
    async def _on_db_connected(payload):
        # Table create + reload of pending rows is sync SQLAlchemy → off the loop.
        await asyncio.to_thread(messaging_gateway.init_db)
        # Start retry workers that were registered before the event loop existed.
        messaging_gateway.start_pending_workers()
        ctx.log.info("Messaging Gateway: DB initialised, retry workers started.")

    @ctx.subscribe("messaging:outbound")
    async def _on_outbound(payload):
        try:
            msg = OutboundMessage(**payload)
            await messaging_gateway.dispatch(msg)
        except Exception as exc:
            ctx.log.error("Messaging Gateway: outbound dispatch error: %s", exc)

    # Housekeeping loops must survive a failing pass: an exception escaping the
    # ``while True`` ended the task for good (logged once), and expired actions /
    # stale streams were never cleaned up again until the next restart.
    async def _expire_loop():
        while True:
            await asyncio.sleep(_EXPIRE_INTERVAL_SECONDS)
            try:
                await messaging_gateway.expire_stale_actions()
            except Exception as exc:  # CancelledError is a BaseException → still stops the loop
                ctx.log.error("Messaging Gateway: expiring stale actions failed: %s", exc)

    async def _stream_cleanup_loop():
        while True:
            await asyncio.sleep(_STREAM_CLEANUP_INTERVAL_SECONDS)
            try:
                await messaging_gateway.stream_bridge.cleanup_stale()
            except Exception as exc:
                ctx.log.error("Messaging Gateway: stream cleanup failed: %s", exc)

    ctx.create_task(_expire_loop(), name="messaging:expire_stale")
    ctx.create_task(_stream_cleanup_loop(), name="messaging:stream_cleanup")

    ctx.log.info("Messaging Gateway: setup complete.")
