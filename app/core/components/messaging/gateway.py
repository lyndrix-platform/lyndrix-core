"""
MessagingGateway — central registry and router for all provider adapters.

Lifecycle
---------
1. On ``db:connected`` the gateway calls ``CorrelationStore.init_db()``.
2. External plugins call ``ctx.register_gateway_adapter(adapter)`` in their
   ``setup()`` hook, which delegates here.
3. Any code emitting ``messaging:outbound`` triggers ``dispatch()``.
4. External providers POST to the adapter's FastAPI route; the route calls
   ``gateway.handle_incoming(provider_id, raw_payload)``.

Dispatch pipeline
-----------------
Each adapter.send() call is bounded by ``adapter.send_timeout`` via
``asyncio.wait_for()``.  This prevents a misbehaving adapter from blocking
the event loop indefinitely.  On timeout or exception the message is
enqueued into a per-adapter ``_AdapterRetryWorker`` for background retry
with exponential backoff (max 3 attempts by default, configurable via
``LYNDRIX_GATEWAY_MAX_RETRY_ATTEMPTS``).

``dispatch()`` returns ``dict[str, DeliveryResult]`` so callers can log or
react to per-adapter outcomes.

Note on ``notification:outbound``
---------------------------------
The gateway does NOT subscribe to or bridge ``notification:outbound``. Internal
notifications reach the bell via the ``InternalNotificationAdapter`` (provider
``"system"``) over ``messaging:outbound``; external delivery is driven by the
notification router. Any adapter that still listens to the legacy
``notification:outbound`` topic does so on its own for backward compatibility.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator
from uuid import uuid4

from core.bus import bus
from .adapter import GatewayAdapter, GatewayCapability
from .correlation import CorrelationStore
from .models import DeliveryResult, InboundMessage, OutboundMessage

log = logging.getLogger("Core:MessagingGateway")


# ---------------------------------------------------------------------------
# Per-adapter background retry worker
# ---------------------------------------------------------------------------

class _AdapterRetryWorker:
    """Background asyncio.Task that retries failed sends with exponential backoff.

    One worker is created per registered adapter.  Workers are started lazily
    once the asyncio event loop is running (``start_pending_workers()``).
    """

    def __init__(self, adapter: GatewayAdapter, max_retries: int, queue_size: int = 100) -> None:
        self._adapter     = adapter
        self._max_retries = max_retries
        self._queue: asyncio.Queue[tuple[OutboundMessage, int]] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"gateway:retry:{self._adapter.provider_id}"
            )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def enqueue(self, message: OutboundMessage, attempt: int) -> bool:
        """Enqueue a message for retry. Returns False if the queue is full."""
        try:
            self._queue.put_nowait((message, attempt))
            return True
        except asyncio.QueueFull:
            log.error(
                "GATEWAY: retry queue full for '%s' — dropping message after %d attempt(s).",
                self._adapter.provider_id, attempt,
            )
            return False

    async def _run(self) -> None:
        while True:
            message, attempt = await self._queue.get()
            delay = min(2 ** (attempt - 1), 30)   # 1s, 2s, 4s, 8s … max 30s
            await asyncio.sleep(delay)

            timeout = getattr(self._adapter, "send_timeout", 30.0)
            try:
                msg_id = await asyncio.wait_for(self._adapter.send(message), timeout=timeout)
                if msg_id is not None:
                    log.info(
                        "GATEWAY: retry %d/%d succeeded for '%s'.",
                        attempt, self._max_retries, self._adapter.provider_id,
                    )
                    continue
                raise RuntimeError("send() returned None")
            except Exception as exc:
                if attempt < self._max_retries:
                    log.warning(
                        "GATEWAY: retry %d/%d failed for '%s': %s — requeueing.",
                        attempt, self._max_retries, self._adapter.provider_id, exc,
                    )
                    self.enqueue(message, attempt + 1)
                else:
                    log.error(
                        "GATEWAY: dead-letter for '%s' after %d attempt(s): %s",
                        self._adapter.provider_id, attempt, exc,
                    )


# ---------------------------------------------------------------------------
# StreamBridge (LLM token streaming bypass)
# ---------------------------------------------------------------------------

class StreamBridge:
    """Bypass for LLM-token streaming — does NOT go through the EventBus.

    The EventBus is a fan-out pub/sub optimised for discrete events.  Sending
    thousands of tiny token messages over it would saturate all subscribers.
    Instead, adapters that declare ``GatewayCapability.STREAMING`` receive a
    ``stream_id`` in ``OutboundMessage.metadata["stream_id"]`` and pull tokens
    directly from this bridge via ``consume()``.

    Usage by a streaming producer::

        stream_id = await stream_bridge.open()
        # include stream_id in OutboundMessage.metadata
        async for token in llm_response:
            await stream_bridge.push(stream_id, token)
        await stream_bridge.close(stream_id)

    Memory safety
    -------------
    ``open()`` enforces ``MAX_STREAMS`` (50) and raises ``RuntimeError`` if the
    limit is reached after attempting ``cleanup_stale()``.  Orphaned streams
    older than ``TTL_SECONDS`` (30 min) are closed by the periodic cleanup timer
    wired in the messaging entrypoint.
    """

    MAX_STREAMS = 50
    TTL_SECONDS = 1800   # 30 minutes

    def __init__(self) -> None:
        self._queues:     dict[str, asyncio.Queue[str | None]] = {}
        self._created_at: dict[str, float] = {}   # monotonic timestamp

    async def open(self) -> str:
        if len(self._queues) >= self.MAX_STREAMS:
            await self.cleanup_stale()
            if len(self._queues) >= self.MAX_STREAMS:
                raise RuntimeError(
                    f"StreamBridge: max open streams ({self.MAX_STREAMS}) reached. "
                    "Ensure all streams are closed after use."
                )
        stream_id = str(uuid4())
        self._queues[stream_id]     = asyncio.Queue()
        self._created_at[stream_id] = time.monotonic()
        return stream_id

    async def push(self, stream_id: str, token: str) -> None:
        q = self._queues.get(stream_id)
        if q:
            await q.put(token)

    async def close(self, stream_id: str) -> None:
        self._created_at.pop(stream_id, None)
        q = self._queues.pop(stream_id, None)
        if q:
            await q.put(None)   # sentinel unblocks waiting consumers

    async def consume(self, stream_id: str) -> AsyncGenerator[str, None]:
        q = self._queues.get(stream_id)
        if not q:
            return
        while True:
            token = await q.get()
            if token is None:   # sentinel → stream finished
                self._queues.pop(stream_id, None)
                self._created_at.pop(stream_id, None)
                break
            yield token

    async def cleanup_stale(self) -> int:
        """Close and remove streams that exceeded TTL. Returns count removed."""
        now = time.monotonic()
        stale = [sid for sid, t in list(self._created_at.items()) if now - t > self.TTL_SECONDS]
        for sid in stale:
            await self.close(sid)
        if stale:
            log.warning("StreamBridge: cleaned up %d orphaned stream(s).", len(stale))
        return len(stale)

    @property
    def open_count(self) -> int:
        return len(self._queues)


# ---------------------------------------------------------------------------
# MessagingGateway
# ---------------------------------------------------------------------------

class MessagingGateway:
    """Central registry and router.  Instantiated once as a module-level singleton."""

    def __init__(self) -> None:
        self._adapters:      dict[str, GatewayAdapter]      = {}
        self._retry_workers: dict[str, _AdapterRetryWorker] = {}
        self._correlation    = CorrelationStore()
        self.stream_bridge   = StreamBridge()
        self._max_retries: int = 3   # default; override via configure()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(self, *, max_retries: int) -> None:
        """Apply runtime configuration before adapters are registered.

        Must be called before ``register()`` so newly created retry workers pick
        up the configured ``max_retries`` value.
        """
        self._max_retries = int(max_retries)

    # ------------------------------------------------------------------
    # Adapter registry
    # ------------------------------------------------------------------

    def register(self, adapter: GatewayAdapter) -> None:
        """Register a provider adapter and create its background retry worker."""
        self._adapters[adapter.provider_id] = adapter
        worker = _AdapterRetryWorker(adapter, max_retries=self._max_retries)
        self._retry_workers[adapter.provider_id] = worker
        # Attempt immediate start; may silently fail before the event loop is ready.
        # start_pending_workers() is called from entrypoint on db:connected.
        try:
            worker.start()
        except RuntimeError:
            pass
        log.info(
            "GATEWAY: Registered adapter '%s' (%s) caps=%s",
            adapter.provider_id,
            adapter.display_name,
            adapter.capabilities,
        )

    def unregister(self, provider_id: str) -> None:
        worker = self._retry_workers.pop(provider_id, None)
        if worker:
            worker.stop()
        removed = self._adapters.pop(provider_id, None)
        if removed:
            log.info("GATEWAY: Unregistered adapter '%s'.", provider_id)

    def start_pending_workers(self) -> None:
        """Start any retry workers whose asyncio task hasn't been created yet.

        Call this once the event loop is running (e.g. on db:connected).
        """
        for worker in self._retry_workers.values():
            if not worker.running:
                worker.start()

    def get_adapter(self, provider_id: str) -> GatewayAdapter | None:
        return self._adapters.get(provider_id)

    def list_adapters(self) -> list[GatewayAdapter]:
        return list(self._adapters.values())

    # ------------------------------------------------------------------
    # DB init (called on db:connected)
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        self._correlation.init_db()

    # ------------------------------------------------------------------
    # Outbound routing
    # ------------------------------------------------------------------

    async def dispatch(self, message: OutboundMessage) -> dict[str, DeliveryResult]:
        """Route *message* to one or all registered adapters.

        Each adapter.send() is wrapped in asyncio.wait_for() using the
        adapter's send_timeout.  Failures are enqueued for background retry.

        Returns a dict mapping provider_id → DeliveryResult so callers can
        log or react to per-adapter outcomes.
        """
        results: dict[str, DeliveryResult] = {}

        if not self._adapters:
            log.debug("GATEWAY: dispatch called but no adapters registered.")
            return results

        # Auto-inject correlation_id when actions are present
        if message.actions and not message.correlation_id:
            message = message.model_copy(update={"correlation_id": uuid4()})

        # Persist pending action when caller provided a resume source
        if message.correlation_id and message.actions and message.source_plugin_id:
            await self._correlation.create(
                correlation_id=message.correlation_id,
                source_plugin_id=message.source_plugin_id,
                resume_topic=f"{message.source_plugin_id.split('.')[-1]}:action_response",
                state_snapshot=message.metadata.get("state_snapshot", {}),
            )
            # Enrich each button with the correlation_id for round-trip resolution
            for btn in message.actions:
                btn.correlation_id = message.correlation_id

        # Select target adapters
        if message.target_provider:
            adapter = self._adapters.get(message.target_provider)
            targets: list[GatewayAdapter] = [adapter] if adapter else []
            if not targets:
                log.warning("GATEWAY: No adapter found for provider '%s'.", message.target_provider)
        else:
            targets = list(self._adapters.values())

        for adapter in targets:
            timeout = getattr(adapter, "send_timeout", 30.0)
            try:
                msg_id = await asyncio.wait_for(adapter.send(message), timeout=timeout)
                if msg_id is not None:
                    results[adapter.provider_id] = DeliveryResult(
                        provider_id=adapter.provider_id,
                        success=True,
                        message_id=msg_id,
                    )
                else:
                    raise RuntimeError("Adapter returned None (delivery failed)")
            except asyncio.TimeoutError:
                error_msg = f"Timed out after {timeout}s"
                worker  = self._retry_workers.get(adapter.provider_id)
                queued  = worker.enqueue(message, attempt=1) if worker else False
                results[adapter.provider_id] = DeliveryResult(
                    provider_id=adapter.provider_id,
                    success=False,
                    error=error_msg,
                    queued_for_retry=queued,
                )
                log.error("GATEWAY: adapter '%s' %s (queued=%s)", adapter.provider_id, error_msg, queued)
            except Exception as exc:
                error_msg = str(exc)
                worker  = self._retry_workers.get(adapter.provider_id)
                queued  = worker.enqueue(message, attempt=1) if worker else False
                results[adapter.provider_id] = DeliveryResult(
                    provider_id=adapter.provider_id,
                    success=False,
                    error=error_msg,
                    queued_for_retry=queued,
                )
                log.error(
                    "GATEWAY: adapter '%s' raised: %s (queued=%s)",
                    adapter.provider_id, exc, queued, exc_info=True,
                )

        return results

    # ------------------------------------------------------------------
    # Inbound routing
    # ------------------------------------------------------------------

    async def handle_incoming(self, provider_id: str, raw: dict) -> None:
        """Dispatch a raw inbound payload from an external provider.

        Typically called by the provider's FastAPI interaction route.
        Resolves the correlation_id and emits the resume topic on the EventBus.
        """
        adapter = self._adapters.get(provider_id)
        if not adapter:
            log.warning("GATEWAY: handle_incoming for unknown provider '%s'.", provider_id)
            return
        if not adapter.supports(GatewayCapability.INTERACTIVE):
            log.warning(
                "GATEWAY: adapter '%s' received incoming payload but lacks INTERACTIVE cap.",
                provider_id,
            )
            return

        try:
            inbound: InboundMessage | None = await adapter.handle_incoming(raw)
        except Exception as exc:
            log.error("GATEWAY: adapter '%s' handle_incoming raised: %s", provider_id, exc)
            return

        if inbound is None:
            return   # adapter signalled "not applicable" (e.g. a ping)

        if not inbound.correlation_id:
            log.debug("GATEWAY: inbound from '%s' has no correlation_id — dropped.", provider_id)
            return

        pending = await self._correlation.resolve(inbound.correlation_id, inbound)
        if pending is None:
            log.warning(
                "GATEWAY: correlation_id %s not found (expired or already resolved).",
                inbound.correlation_id,
            )
            return

        bus.emit(
            pending.resume_topic,
            {
                "correlation_id": str(inbound.correlation_id),
                "action_id":      inbound.action_id,
                "user_id":        inbound.user_id,
                "text_response":  inbound.text_response,
                "state":          pending.state_snapshot,
                "response":       inbound.model_dump(mode="json"),
            },
        )
        log.info(
            "GATEWAY: Resolved correlation %s → emitted '%s'.",
            inbound.correlation_id,
            pending.resume_topic,
        )

    # ------------------------------------------------------------------
    # Per-provider health check
    # ------------------------------------------------------------------

    async def provider_health(self, provider_id: str) -> bool | None:
        """Call adapter.health() with a 5-second timeout.

        Returns True/False on success/failure, None if the provider is not
        registered.
        """
        adapter = self._adapters.get(provider_id)
        if not adapter:
            return None
        try:
            return await asyncio.wait_for(adapter.health(), timeout=5.0)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Periodic maintenance
    # ------------------------------------------------------------------

    async def expire_stale_actions(self) -> None:
        await self._correlation.expire_stale()


# Module-level singleton — imported by entrypoint, adapters, and core/api.
messaging_gateway = MessagingGateway()
