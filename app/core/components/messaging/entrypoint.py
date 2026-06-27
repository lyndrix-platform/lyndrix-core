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
# TODO(agent): reshape this flat component to the canonical anatomy
# (model/: models.py + pending_action.py; logic/: gateway.py, correlation.py,
# adapter.py, internal_adapter.py). Deferred: the move ripples into the stable
# core.api re-export surface (core/api/__init__.py imports the adapter ABC,
# models, CorrelationStore and gateway by path) and dependencies are not
# installed here, so the import rewrite cannot be runtime-verified in this pass.
from core.api import ModuleManifest
from .gateway import messaging_gateway
from .internal_adapter import InternalNotificationAdapter
from .models import OutboundMessage

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
        messaging_gateway.init_db()
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

    import asyncio

    async def _expire_loop():
        while True:
            await asyncio.sleep(300)
            await messaging_gateway.expire_stale_actions()

    async def _stream_cleanup_loop():
        while True:
            await asyncio.sleep(600)
            await messaging_gateway.stream_bridge.cleanup_stale()

    ctx.create_task(_expire_loop(), name="messaging:expire_stale")
    ctx.create_task(_stream_cleanup_loop(), name="messaging:stream_cleanup")

    ctx.log.info("Messaging Gateway: setup complete.")
