"""
Server-Sent Events endpoint for real-time platform updates.

Each active SSE connection registers an asyncio.Queue in _SSE_QUEUES.
Module-level bus subscribers forward whitelisted topics to all queues.
"""
import asyncio
import json
from typing import Set

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.api.security import ApiIdentity, optional_api_auth
from fastapi import Depends
from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:EventsAPI")

events_router = APIRouter(tags=["Events"])

_SSE_QUEUES: Set[asyncio.Queue] = set()

# Topics sent to authenticated clients.
_AUTH_TOPICS = frozenset({
    "system:boot_complete",
    "plugin:state_changed",
    "ui:needs_refresh",
    "system:metrics_update",
    "vault:status_changed",
    "notification:new",
})

# Subset sent to unauthenticated clients.
_PUBLIC_TOPICS = frozenset({
    "system:boot_complete",
    "vault:status_changed",
})


def _publish(topic: str, payload: dict) -> None:
    if not _SSE_QUEUES:
        return
    msg = json.dumps({"topic": topic, "payload": payload})
    dead: Set[asyncio.Queue] = set()
    for q in _SSE_QUEUES:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.add(q)
    _SSE_QUEUES.difference_update(dead)


def _make_forwarder(topic: str):
    def _forward(payload: dict = None):
        _publish(topic, payload or {})
    _forward.__qualname__ = f"_sse_forward_{topic.replace(':', '_')}"
    _forward.__module__ = __name__
    return _forward


for _topic in _AUTH_TOPICS:
    bus.subscribe(_topic)(_make_forwarder(_topic))


@events_router.get("/api/events", summary="Server-Sent Events stream")
async def sse_stream(
    request: Request,
    identity: ApiIdentity = Depends(optional_api_auth),
):
    """
    Real-time event stream over Server-Sent Events (SSE).

    Authenticated clients receive all whitelisted bus topics.
    Unauthenticated clients receive only ``system:boot_complete`` and
    ``vault:status_changed``.

    Each event payload is a JSON object: ``{"topic": "...", "payload": {...}}``.
    """
    allowed = _AUTH_TOPICS if identity else _PUBLIC_TOPICS
    username = identity.username if identity else None
    q: asyncio.Queue = asyncio.Queue(maxsize=128)
    _SSE_QUEUES.add(q)

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                try:
                    data = json.loads(msg)
                except Exception:
                    # Unparseable message — forward best-effort and move on.
                    yield f"data: {msg}\n\n"
                    continue

                topic = data.get("topic")
                if topic not in allowed:
                    continue

                # Per-connection user scoping for user-targeted topics: a notification
                # is delivered only to its owner (payload.user_id == this connection's
                # username) or to everyone when it is a broadcast (user_id is None).
                # Without this, the shared broadcast queue would stream user A's
                # notification body to user B. Also strip the internal user_id so it is
                # never exposed to clients (mirrors the REST _public_view contract).
                if topic == "notification:new":
                    payload = data.get("payload") or {}
                    target = payload.get("user_id")
                    if target is not None and target != username:
                        continue
                    if "user_id" in payload:
                        sanitized = {k: v for k, v in payload.items() if k != "user_id"}
                        msg = json.dumps({"topic": topic, "payload": sanitized})

                yield f"data: {msg}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            _SSE_QUEUES.discard(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
