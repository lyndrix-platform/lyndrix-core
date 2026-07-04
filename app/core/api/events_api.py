"""
Server-Sent Events endpoint for real-time platform updates.

Each active SSE connection registers an asyncio.Queue in _SSE_QUEUES.
Module-level bus subscribers forward whitelisted topics to all queues.

Browser ``EventSource`` cannot send an Authorization header, so header-based
auth alone would leave every React client on the public topic subset. To grant
browsers the authenticated stream without leaking the long-lived session bearer
into URLs, an authenticated client first mints a short-TTL HMAC ticket via
``POST /api/events/ticket`` and passes it as ``GET /api/events?ticket=…``.
The signing secret is process-random: tickets only need to outlive the few
seconds between mint and connect, and a restart invalidating them is fine.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional, Set

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.api.security import ApiIdentity, optional_api_auth, require_api_auth
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
    "plugin:installed",
    "plugin:install_failed",
    "plugin:update_available",
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


# ─── Stream tickets ──────────────────────────────────────────────────────────

_TICKET_SECRET = secrets.token_bytes(32)
_TICKET_TTL_SECONDS = 60


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _connection_allows(username: str | None, permission: str) -> bool:
    """Audience gate for the SSE stream (superadmin flag included)."""
    if not username:
        return False
    try:
        from core.components.auth.logic import access_service
        from core.components.auth.logic.user_service import user_service

        user = user_service.get_by_username(username)
        if user is not None and "superadmin" in (user.roles or []):
            return True
        return access_service.username_has_permission(username, permission)
    except Exception:
        return False


def _mint_stream_ticket(username: str) -> str:
    payload = _b64url_encode(
        json.dumps({"u": username, "exp": int(time.time()) + _TICKET_TTL_SECONDS}).encode("utf-8")
    )
    sig = hmac.new(_TICKET_SECRET, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_stream_ticket(ticket: str) -> Optional[str]:
    """Return the ticket's username when valid and unexpired, else None."""
    try:
        payload, sig = ticket.split(".", 1)
        expected = hmac.new(_TICKET_SECRET, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64url_decode(payload))
        if int(data.get("exp", 0)) < time.time():
            return None
        username = data.get("u")
        return username if isinstance(username, str) and username else None
    except Exception:
        return None


@events_router.post("/api/events/ticket", summary="Mint a short-lived SSE stream ticket")
async def mint_stream_ticket(identity: ApiIdentity = Depends(require_api_auth)):
    """
    Mint a short-TTL ticket granting one authenticated ``/api/events`` connection.

    Browser ``EventSource`` cannot send an Authorization header; passing the
    long-lived session bearer as a query param would leak it into access logs.
    Clients call this endpoint (header-authenticated) right before connecting and
    pass the returned ticket as ``/api/events?ticket=…``. Tickets are process-local
    and expire after ``expires_in`` seconds — mint a fresh one per (re)connect.
    """
    return {"ticket": _mint_stream_ticket(identity.username), "expires_in": _TICKET_TTL_SECONDS}


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
    ticket: Optional[str] = None,
    identity: ApiIdentity = Depends(optional_api_auth),
):
    """
    Real-time event stream over Server-Sent Events (SSE).

    Authenticated clients receive all whitelisted bus topics.
    Unauthenticated clients receive only ``system:boot_complete`` and
    ``vault:status_changed``.

    Authentication: standard header auth (Bearer / X-API-Key), or a short-TTL
    ``?ticket=`` minted via ``POST /api/events/ticket`` — the only way a browser
    ``EventSource`` (which cannot send headers) can join the authenticated stream.

    Each event payload is a JSON object: ``{"topic": "...", "payload": {...}}``.
    """
    ticket_user = _verify_stream_ticket(ticket) if ticket else None
    allowed = _AUTH_TOPICS if (identity or ticket_user) else _PUBLIC_TOPICS
    username = identity.username if identity else ticket_user
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
                    # Routing v2 audience gate: permission-restricted entries
                    # only reach connections whose user holds the permission
                    # (resolved per message via the cached access_service —
                    # 5s TTL keeps this cheap; notification volume is low).
                    req = payload.get("required_permission")
                    if req and not _connection_allows(username, req):
                        continue
                    internal = {"user_id", "required_permission", "source_plugin_id", "endpoint_name"}
                    if any(k in payload for k in internal):
                        sanitized = {k: v for k, v in payload.items() if k not in internal}
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
