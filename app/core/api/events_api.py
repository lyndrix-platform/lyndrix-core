"""
Server-Sent Events endpoint for real-time platform updates.

Each active SSE connection registers an asyncio.Queue in _SSE_QUEUES.
Module-level bus subscribers forward whitelisted topics to all queues.

Browser ``EventSource`` cannot send an Authorization header, so header-based
auth alone would leave every React client on the public topic subset. To grant
browsers the authenticated stream without leaking the long-lived session bearer
into URLs, an authenticated client first mints a short-TTL HMAC ticket via
``POST /api/events/ticket`` and passes it as ``GET /api/events?ticket=…``.
The signing secret is derived deterministically from ``STORAGE_SECRET`` (see
``_derive_ticket_secret``) so tickets stay valid across a restart or across
replicas sharing the same secret; it only falls back to a process-random
value when ``STORAGE_SECRET`` is unset.

Who gets what is decided per connection from an *audience snapshot* (credential
still valid? user still there? effective permissions) that is loaded off the
event loop at connect and refreshed every ``_AUDIENCE_REFRESH_SECONDS``. Topic
and notification gates are then pure in-memory checks per message.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
import weakref
from dataclasses import dataclass
from typing import Any, Optional, Set

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.api.security import ApiIdentity, authenticate_request, optional_api_auth, require_api_auth
from fastapi import Depends
from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:EventsAPI")

events_router = APIRouter(tags=["Events"])

_SSE_QUEUES: Set[asyncio.Queue] = set()

# Who opened which stream ("user:<name>" / "addr:<client address>"), for the
# per-user connection cap. Weak keys and counted against _SSE_QUEUES, so an
# entry can never outlive its stream and keep somebody locked out.
_SSE_OWNERS: "weakref.WeakKeyDictionary[asyncio.Queue, str]" = weakref.WeakKeyDictionary()
# Streams without a login (boot/login/unseal pages) share a client address
# behind NAT or an untrusted proxy — their cap is this many times the user cap.
_ANONYMOUS_CAP_FACTOR = 5


def _admit_stream(owner: str, *, anonymous: bool) -> None:
    """Refuse another stream for ``owner`` beyond the cap (429).

    Every open stream holds a queue, a task and a socket; without a cap one
    account (or one unauthenticated client) could open them without bound.
    The shell uses one stream per tab and retries with backoff, falling back
    to polling meanwhile.
    """
    try:
        from config import settings

        cap = int(getattr(settings, "LYNDRIX_SSE_MAX_CONNECTIONS_PER_USER", 0) or 0)
    except Exception:  # pragma: no cover - defensive
        cap = 0
    if cap <= 0:
        return
    if anonymous:
        cap *= _ANONYMOUS_CAP_FACTOR
    open_streams = sum(1 for q, who in list(_SSE_OWNERS.items()) if who == owner and q in _SSE_QUEUES)
    if open_streams >= cap:
        raise HTTPException(
            status_code=429,
            detail="Too many open event streams for this account. Close a tab or try again shortly.",
            headers={"Retry-After": "30"},
        )

# Topics sent to authenticated clients.
_AUTH_TOPICS = frozenset({
    "system:boot_complete",
    "plugin:state_changed",
    "plugin:runtime_status",
    "plugin:installed",
    "plugin:install_failed",
    "plugin:update_available",
    "nav:changed",
    "plugin:operation_changed",
    "ui:needs_refresh",
    "system:metrics_update",
    "vault:status_changed",
    "notification:new",
    "cluster:changed",
    "cluster:leader_changed",
    "system:draining",
})

# Subset sent to unauthenticated clients.
_PUBLIC_TOPICS = frozenset({
    "system:boot_complete",
    "vault:status_changed",
})

# Per-topic permission gate (any one of the listed permissions suffices;
# superadmin / the system key pass). Plugin install/operation events carry repo
# names, paths and error text, cluster events the topology — operator data that
# used to reach every authenticated stream (audit finding AZ-06).
_PLUGIN_ADMIN = ("feature:plugins.manage", "admin:read")
_TOPIC_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "plugin:installed": _PLUGIN_ADMIN,
    "plugin:install_failed": _PLUGIN_ADMIN,
    "plugin:update_available": _PLUGIN_ADMIN,
    "plugin:operation_changed": _PLUGIN_ADMIN,   # REST /api/plugins/operations needs admin:read
    "cluster:changed": ("admin:read",),
    "cluster:leader_changed": ("admin:read",),
}
# Topics every signed-in shell needs to stay consistent (plugin module cache,
# sidebar, i18n/theme refresh), but whose payload may say more than a user
# without plugin-admin rights should see. Those connections get only the
# listed keys — enough for the React shell, which reads ``plugin_id`` alone.
# ``plugin:runtime_status`` (warm-up panel) and ``nav:changed`` flow unchanged.
_REDUCED_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "plugin:state_changed": ("plugin_id", "action", "is_active"),
    "ui:needs_refresh": (),
}
# Keys of a ``notification:new`` payload that drive delivery and never leave the server.
_NOTIFICATION_INTERNAL_KEYS = frozenset({"user_id", "required_permission", "source_plugin_id", "endpoint_name"})

_QUEUE_SIZE = 128
# Lossy high-frequency topic: not queued for a connection that is already
# falling behind, so it cannot push a slow client into an overflow by itself.
_LOSSY_TOPICS = frozenset({"system:metrics_update"})

# The audience snapshot is refreshed when older than this. The stream wakes at
# least every keep-alive interval (15 s) and checks before each delivery, so a
# revoked credential, a deleted user or a withdrawn permission takes effect
# within ~20 s (plus the 5 s access_service cache) — always under 30 s.
_AUDIENCE_REFRESH_SECONDS = 20.0
_KEEPALIVE_SECONDS = 15.0

# Internal queue message that ends a stream (drain / shutdown / overflow). The
# generator sends a final ``event: reconnect`` and ENDS THE RESPONSE. The end of
# the stream is what makes clients reconnect: the React shell (useSSE) has no
# listener for the custom event, its ``onerror`` fires when the stream closes
# and schedules a reconnect with a fresh ticket.
_CLOSE_KEY = "__close__"


def _force_close(q: asyncio.Queue, reason: str) -> None:
    """Take ``q`` out of the fan-out and make its stream end next.

    The backlog is dropped so the close sentinel always fits and is the very
    next thing the generator sees — the client resyncs on reconnect anyway.
    """
    _SSE_QUEUES.discard(q)
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
    q.put_nowait(json.dumps({_CLOSE_KEY: reason}))


def close_all_streams(reason: str = "reconnect") -> int:
    """Ask every open SSE connection to close with a reconnect hint."""
    closed = 0
    for q in list(_SSE_QUEUES):
        try:
            q.put_nowait(json.dumps({_CLOSE_KEY: reason}))
        except asyncio.QueueFull:
            _force_close(q, reason)
        closed += 1
    if closed:
        log.info("SSE: closing %d stream(s) (%s)", closed, reason)
    return closed


# ─── Stream tickets ──────────────────────────────────────────────────────────

_TICKET_CONTEXT = b"lyndrix-sse-ticket-v1"


def _derive_ticket_secret() -> bytes:
    """Derive the SSE ticket HMAC key from ``STORAGE_SECRET``.

    Deterministic across restarts/replicas (unlike a fresh random secret per
    process) since it's keyed off the same secret every instance already
    shares. Falls back to a process-random secret when ``STORAGE_SECRET`` is
    unset/empty — tickets then only work within this single process, exactly
    the previous behavior.
    """
    try:
        from config import settings

        storage_secret = (settings.STORAGE_SECRET or "").strip()
    except Exception:  # pragma: no cover - defensive, e.g. import-order edge case
        storage_secret = ""
    if not storage_secret:
        return secrets.token_bytes(32)
    return hmac.new(storage_secret.encode("utf-8"), _TICKET_CONTEXT, hashlib.sha256).digest()


_TICKET_SECRET = _derive_ticket_secret()
_TICKET_TTL_SECONDS = 60


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# ─── Audience snapshot ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Credential:
    """What a connection authenticated with — enough to re-validate it later."""

    username: str
    is_system: bool = False               # master system API key
    key_id: Optional[int] = None          # per-user API key behind a ticket
    request: Optional[Request] = None     # header-authenticated: re-run the auth chain


@dataclass(frozen=True)
class _Audience:
    """One connection's rights at a point in time (checked in memory per message)."""

    valid: bool = True                    # False → credential revoked / user gone: end the stream
    superadmin: bool = False
    permissions: frozenset = frozenset()
    scopes: Optional[frozenset] = None    # scoped per-user API key

    def allows(self, permission: str) -> bool:
        if self.superadmin:
            return True
        from core.components.auth.logic import access_service

        if not access_service.has_permission(self.permissions, permission):
            return False
        return self.scopes is None or access_service.has_permission(self.scopes, permission)

    def allows_any(self, permissions: tuple[str, ...]) -> bool:
        return any(self.allows(p) for p in permissions)


_REVOKED = _Audience(valid=False)
# Rights unknown (DB error during a refresh): fail closed for everything gated,
# keep the stream itself — it recovers with the next successful refresh.
_UNKNOWN = _Audience()


def _api_key_scopes(key_id: int, username: str) -> Optional[frozenset]:
    """Scopes of a still-valid per-user key (``frozenset()`` = unscoped); ``None`` = revoked/gone."""
    from core.components.auth.logic.models import UserApiKey
    from core.components.database.logic.db_service import db_instance

    with db_instance.SessionLocal() as s:
        rec = (
            s.query(UserApiKey)
            .filter(UserApiKey.id == key_id, UserApiKey.username == username,
                    UserApiKey.revoked == False)  # noqa: E712
            .first()
        )
        return None if rec is None else frozenset(rec.scopes or [])


def _api_key_id(request: Request) -> Optional[int]:
    """Id of the per-user API key this request presented (read-only lookup), if any."""
    import hashlib as _hashlib

    from core.components.auth.logic.models import UserApiKey
    from core.components.database.logic.db_service import db_instance

    auth = request.headers.get("Authorization", "")
    presented = request.headers.get("X-API-Key") or (auth[7:] if auth.lower().startswith("bearer ") else "")
    presented = presented.strip()
    if not presented or db_instance.SessionLocal is None:
        return None
    digest = _hashlib.sha256(presented.encode("utf-8")).hexdigest()
    with db_instance.SessionLocal() as s:
        rec = s.query(UserApiKey.id).filter(
            UserApiKey.hashed_key == digest, UserApiKey.revoked == False  # noqa: E712
        ).first()
        return int(rec[0]) if rec else None


def _load_audience(cred: _Credential) -> _Audience:
    """Blocking (DB): re-validate the credential and resolve the rights. Run via ``to_thread``.

    Raises on infrastructure errors — the caller decides how to degrade.
    """
    from core.components.auth.logic import access_service
    from core.components.auth.logic.user_service import user_service

    if cred.request is not None:
        # Header-authenticated stream: the full auth chain again, so a revoked
        # API key or a removed system key ends the stream.
        identity = authenticate_request(cred.request)
        if identity is None:
            return _REVOKED
        if identity.is_system:
            return _Audience(superadmin=True)
        username = identity.username
        scopes = frozenset(identity.key_scopes) if identity.key_scopes is not None else None
    elif cred.is_system:
        # Ticket minted with the system key: the key itself is not in the
        # ticket, so there is nothing to re-check beyond the ticket signature.
        return _Audience(superadmin=True)
    else:
        username = cred.username
        scopes = None
        if cred.key_id is not None:
            key_scopes = _api_key_scopes(cred.key_id, username)
            if key_scopes is None:
                return _REVOKED
            scopes = key_scopes or None

    user = user_service.get_by_username(username)
    if user is None:
        return _REVOKED  # deleted user: no stream, whatever the ticket says
    return _Audience(
        superadmin="superadmin" in (user.roles or []),
        permissions=access_service.get_effective_permissions(username),
        scopes=scopes,
    )


async def _refresh_audience(cred: _Credential) -> _Audience:
    try:
        return await asyncio.to_thread(_load_audience, cred)
    except Exception as exc:
        log.warning("SSE: audience refresh failed for '%s' (%s) — gated topics paused", cred.username, exc)
        return _UNKNOWN


def _may_join_authenticated_stream(identity: Optional[ApiIdentity]) -> bool:
    """Whether a header-authenticated caller gets the authenticated topic set.

    A ticket (and the stream opened with it) carries the user name only, so a
    scope-restricted key would receive everything its owner may see. It is
    therefore admitted only when its scopes cover general read access.
    """
    if identity is None:
        return False
    return not identity.is_scoped or identity.allows("api:read")


def _mint_stream_ticket(username: str, *, is_system: bool = False, key_id: Optional[int] = None) -> str:
    claims: dict[str, Any] = {"u": username, "exp": int(time.time()) + _TICKET_TTL_SECONDS}
    if is_system:
        claims["s"] = 1
    if key_id is not None:
        # Row id of the API key (not secret) — lets the stream notice a revocation.
        claims["k"] = int(key_id)
    payload = _b64url_encode(json.dumps(claims).encode("utf-8"))
    sig = hmac.new(_TICKET_SECRET, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_stream_ticket_claims(ticket: str) -> Optional[dict]:
    """Return the claims of a valid, unexpired ticket, else None."""
    try:
        payload, sig = ticket.split(".", 1)
        expected = hmac.new(_TICKET_SECRET, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64url_decode(payload))
        if int(data.get("exp", 0)) < time.time():
            return None
        username = data.get("u")
        return data if isinstance(username, str) and username else None
    except Exception:
        return None


def _verify_stream_ticket(ticket: str) -> Optional[str]:
    """Return the ticket's username when valid and unexpired, else None."""
    claims = _verify_stream_ticket_claims(ticket)
    return claims["u"] if claims else None


def _credential_from_ticket(claims: dict) -> _Credential:
    key_id = claims.get("k")
    return _Credential(
        username=claims["u"],
        is_system=bool(claims.get("s")),
        key_id=int(key_id) if isinstance(key_id, int) else None,
    )


@events_router.post("/api/events/ticket", summary="Mint a short-lived SSE stream ticket")
async def mint_stream_ticket(request: Request, identity: ApiIdentity = Depends(require_api_auth)):
    """
    Mint a short-TTL ticket granting one authenticated ``/api/events`` connection.

    Browser ``EventSource`` cannot send an Authorization header; passing the
    long-lived session bearer as a query param would leak it into access logs.
    Clients call this endpoint (header-authenticated) right before connecting and
    pass the returned ticket as ``/api/events?ticket=…``. Tickets are process-local
    and expire after ``expires_in`` seconds — mint a fresh one per (re)connect.

    A scope-restricted API key gets a ticket only when its scopes include
    ``api:read`` (403 otherwise): the ticket stands for the whole account.
    """
    if not _may_join_authenticated_stream(identity):
        raise HTTPException(
            status_code=403,
            detail="This API key's scopes do not include api:read — no authenticated event stream",
        )
    key_id: Optional[int] = None
    if identity.method == "user_api_key":
        try:
            key_id = await asyncio.to_thread(_api_key_id, request)
        except Exception as exc:  # pragma: no cover - defensive: the ticket still works
            log.debug("SSE: could not resolve API key id for ticket: %s", exc)
    ticket = _mint_stream_ticket(identity.username, is_system=identity.is_system, key_id=key_id)
    return {"ticket": ticket, "expires_in": _TICKET_TTL_SECONDS}


def _publish(topic: str, payload: dict) -> None:
    if not _SSE_QUEUES:
        return
    msg = json.dumps({"topic": topic, "payload": payload})
    lossy = topic in _LOSSY_TOPICS
    for q in list(_SSE_QUEUES):
        if lossy and q.qsize() > q.maxsize // 2:
            continue
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # A client that stopped reading. Dropping the queue from the fan-out
            # alone left a zombie: the stream stayed open, got keep-alives and
            # never another event, so EventSource never reconnected. End it.
            log.info("SSE: client queue overflowed — closing the stream so the client reconnects")
            _force_close(q, "overflow")


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
    if identity is not None and not _may_join_authenticated_stream(identity):
        identity = None  # scoped key without api:read → public topics only
    claims = _verify_stream_ticket_claims(ticket) if ticket else None
    cred: Optional[_Credential] = None
    if identity is not None:
        cred = _Credential(username=identity.username, request=request)
    elif claims is not None:
        cred = _credential_from_ticket(claims)

    # Resolve who is listening BEFORE anything is delivered. A ticket for a
    # user that no longer exists (or a key revoked since) gets the public subset.
    if identity is not None:
        # Just authenticated by the dependency — no second round trip.
        audience = _Audience(
            superadmin=identity.is_system or "superadmin" in (identity.roles or []),
            permissions=await asyncio.to_thread(identity.effective_permissions),
            scopes=frozenset(identity.key_scopes) if identity.key_scopes is not None else None,
        )
    else:
        audience = await _refresh_audience(cred) if cred else _UNKNOWN
    if cred is not None and not audience.valid:
        cred = None
    allowed = _AUTH_TOPICS if cred else _PUBLIC_TOPICS
    username = cred.username if cred else None

    # Per-user (or per-address, for anonymous streams) cap on open streams (AZ-11).
    if username:
        owner = f"user:{username}"
    else:
        from core.components.vault.logic.rate_limit import normalize_client_address

        owner = f"addr:{normalize_client_address(request.client.host if request.client else None)}"
    _admit_stream(owner, anonymous=not username)

    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    _SSE_QUEUES.add(q)
    _SSE_OWNERS[q] = owner

    return StreamingResponse(
        _stream(request, q, allowed, username, cred, audience),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _reconnect_frame(reason: str) -> str:
    try:
        from core.components.cluster.logic.cluster_service import cluster_service

        instance_id = cluster_service.instance_id
    except Exception:  # pragma: no cover - defensive
        instance_id = None
    return f"event: reconnect\ndata: {json.dumps({'reason': reason, 'instance_id': instance_id})}\n\n"


def _render(topic: str, data: dict, msg: str, username: Optional[str], audience: _Audience) -> Optional[str]:
    """The frame for one bus message on one connection, or ``None`` to skip it."""
    required = _TOPIC_PERMISSIONS.get(topic)
    if required and not audience.allows_any(required):
        return None

    payload = data.get("payload") or {}

    if topic in _REDUCED_PAYLOAD_KEYS and not audience.allows_any(_PLUGIN_ADMIN):
        keep = _REDUCED_PAYLOAD_KEYS[topic]
        reduced = {k: payload[k] for k in keep if k in payload}
        return f"data: {json.dumps({'topic': topic, 'payload': reduced})}\n\n"

    # Per-connection user scoping for user-targeted topics: a notification is
    # delivered only to its owner (payload.user_id == this connection's
    # username) or to everyone when it is a broadcast (user_id is None).
    # Without this, the shared broadcast queue would stream user A's
    # notification body to user B. The internal keys are stripped so they are
    # never exposed to clients (mirrors the REST _public_view contract).
    if topic == "notification:new":
        target = payload.get("user_id")
        if target is not None and target != username:
            return None
        # Routing v2 audience gate: permission-restricted entries only reach
        # connections whose user holds the permission (in-memory snapshot).
        req = payload.get("required_permission")
        if req and not audience.allows(req):
            return None
        if any(k in payload for k in _NOTIFICATION_INTERNAL_KEYS):
            sanitized = {k: v for k, v in payload.items() if k not in _NOTIFICATION_INTERNAL_KEYS}
            return f"data: {json.dumps({'topic': topic, 'payload': sanitized})}\n\n"

    return f"data: {msg}\n\n"


async def _stream(
    request: Request,
    q: asyncio.Queue,
    allowed: frozenset,
    username: Optional[str],
    cred: Optional[_Credential],
    audience: _Audience,
):
    """The SSE body of one connection."""
    refreshed_at = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                msg: Optional[str] = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                msg = None

            # Refresh the snapshot off-loop when it is due — on idle wake-ups
            # too, so a revoked stream ends even when nothing is being sent.
            if cred is not None and time.monotonic() - refreshed_at >= _AUDIENCE_REFRESH_SECONDS:
                audience = await _refresh_audience(cred)
                refreshed_at = time.monotonic()
                if not audience.valid:
                    # Credential revoked / user deleted: end the stream. The
                    # client's reconnect cannot mint a ticket any more.
                    yield _reconnect_frame("revoked")
                    break

            if msg is None:
                yield ": keep-alive\n\n"
                continue

            try:
                data = json.loads(msg)
            except Exception:
                # Unparseable message — forward best-effort and move on.
                yield f"data: {msg}\n\n"
                continue

            if _CLOSE_KEY in data:
                yield _reconnect_frame(data[_CLOSE_KEY])
                break

            topic = data.get("topic")
            if topic not in allowed:
                continue
            frame = _render(topic, data, msg, username, audience)
            if frame is not None:
                yield frame
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        _SSE_QUEUES.discard(q)
