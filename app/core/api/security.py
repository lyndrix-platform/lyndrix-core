"""
Lyndrix Core API authentication.

Provides a small, composable auth layer for FastAPI/HTTP endpoints that is
compatible with the existing multi-provider auth system.  Multiple credential
mechanisms are accepted so a request can authenticate in whichever way fits the
caller:

  1. System API key   — header ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
                         Resolved from the ``LYNDRIX_SYSTEM_API_KEY`` env var first,
                         then Vault (``lyndrix/core/settings`` → ``system_api_key``).
                         If neither is configured the method is DISABLED: no empty
                         or implicit key is ever accepted, so a fresh install with
                         the key unset is simply inaccessible via API key.
  2. Per-user API key — same headers, a ``lyk_…`` key or session token from the
                         key store (``api_key_service``).
  3. HTTP Basic       — ``Authorization: Basic <base64(user:pass)>`` validated
                         against the LOCAL accounts via
                         ``auth_service.verify_local_login``. Throttled like the
                         login form (429/503 + ``Retry-After`` instead of 401
                         when an attempt is not evaluated), only on routes that
                         REQUIRE authentication, and switched off with
                         ``LYNDRIX_API_BASIC_AUTH=false``. Not advertised in
                         ``WWW-Authenticate`` — a browser must never prompt for
                         it and then replay the password with every request.

Usage in a route::

    from core.api import require_api_auth, ApiIdentity

    @app.get("/api/secure", tags=["System"])
    async def secure(identity: ApiIdentity = Depends(require_api_auth)):
        return {"who": identity.username, "via": identity.method}

For endpoints that should stay public but still surface the caller when present,
use ``optional_api_auth`` instead.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import HTTPException, Request, status

from core.logger import get_logger

log = get_logger("Core:ApiSecurity")

_VAULT_PATH = "core/settings"
_VAULT_MOUNT = "lyndrix"
_VAULT_KEY = "system_api_key"
_ENV_VAR = "LYNDRIX_SYSTEM_API_KEY"

# Short-lived cache for the Vault-resolved system API key so an authenticated
# request does not trigger a fresh hvac round-trip every time. Env-var resolution
# is not cached (cheap + always authoritative). Bust explicitly on settings update.
_KEY_CACHE_TTL_SECONDS = 30.0
# After a failed Vault read the next attempt is allowed this soon (retry throttle).
_KEY_ERROR_RETRY_SECONDS = 2.0
_key_cache: dict = {"value": None, "expires": 0.0}


def invalidate_system_api_key_cache() -> None:
    """Drop the cached Vault key so the next resolve re-reads from Vault.

    Called by ``system_config_service.apply`` whenever the key is written or
    cleared, and on every peer instance through the bridged
    ``settings:changed`` event, so a rotated key stops working at once.
    """
    _key_cache["value"] = None
    _key_cache["expires"] = 0.0


@dataclass
class ApiIdentity:
    """Resolved identity for an authenticated API request.

    ``permissions`` is the owner's precomputed effective set (groups-union ∪
    extra_permissions, resolved via ``access_service`` by the ``_try_*``
    builders). This is the Identity 2.0 fix for the long-standing bug where the
    API path only ever read ``User.roles`` and group membership did nothing.
    """

    method: str                       # "api_key" | "user_api_key" | "basic" | "session"
    username: str = "system"
    roles: List[str] = field(default_factory=list)
    extra_permissions: List[str] = field(default_factory=list)
    is_system: bool = False           # True for the master system API key
    # Precomputed effective permission set. None = resolve lazily by username
    # (safety net for manually constructed identities).
    permissions: Optional[frozenset] = None
    # When set (non-None), the credential restricts access to this subset of
    # permissions (used by per-user API keys with explicit scopes).
    key_scopes: Optional[List[str]] = None
    # Row id of the presented per-user key (None for other methods) — lets a
    # route tell "this very credential" from the owner's other keys.
    key_id: Optional[int] = None

    @property
    def is_scoped(self) -> bool:
        """True for a credential that is limited to ``key_scopes``."""
        return self.key_scopes is not None

    @property
    def is_superadmin(self) -> bool:
        """The unbounded tier: the system key, or an UNSCOPED credential of an
        account with the ``superadmin`` flag. A scoped key of such an account
        is bounded by its scopes and never counts as superadmin."""
        return self.is_system or ("superadmin" in (self.roles or []) and not self.is_scoped)

    def effective_permissions(self) -> frozenset:
        """The owner's effective permission set (resolving lazily if needed)."""
        if self.permissions is not None:
            return self.permissions
        try:
            from core.components.auth.logic import access_service

            return access_service.get_effective_permissions(self.username)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(f"API authz resolution failed for '{self.username}': {exc}")
            return frozenset()

    def allows(self, permission: str) -> bool:
        """
        Return True if this identity is authorized for ``permission``.

        - The master system key bypasses all permission checks.
        - A scoped per-user key FIRST requires the permission to be within its
          ``key_scopes`` (per-plugin fallback included) — whoever the owner
          is. Scopes are the boundary of the credential, so they are applied
          before the superadmin bypass: a superadmin's "read-only" key is
          read-only.
        - The ``superadmin`` system flag then bypasses the owner's permission
          set (break-glass).
        - Otherwise the precomputed effective set decides, including the
          per-plugin fallback (global api:read/write satisfies plugin:*:api:*).
        """
        if self.is_system:
            return True

        try:
            from core.components.auth.logic import access_service

            if self.key_scopes is not None and not access_service.has_permission(
                frozenset(self.key_scopes), permission
            ):
                return False
            if "superadmin" in self.roles:
                return True
            return access_service.has_permission(self.effective_permissions(), permission)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(f"API authz check failed for '{permission}': {exc}")
            return False


def resolve_system_api_key() -> Optional[str]:
    """
    Return the effective system API key, or ``None`` when it is not configured.

    Resolution order (highest priority first):
      1. ``LYNDRIX_SYSTEM_API_KEY`` environment variable
      2. Vault  ``lyndrix/core/settings`` → ``system_api_key``

    A configured-but-empty value is treated as *not configured* so the API-key
    method cannot be silently enabled with a blank key.
    """
    env_val = os.getenv(_ENV_VAR)
    if env_val and env_val.strip():
        return env_val.strip()

    # Serve a recently resolved Vault value to avoid a round-trip per request.
    now = time.monotonic()
    if _key_cache["expires"] > now:
        return _key_cache["value"]

    value: Optional[str] = None
    try:
        import hvac.exceptions

        from core.services import vault_instance

        if not vault_instance.is_connected:
            # Vault state unknown (sealed / not up yet): answer "not configured"
            # for THIS call only. Caching it would keep API-key auth off for the
            # whole TTL after Vault is back.
            return None
        try:
            resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                path=_VAULT_PATH, mount_point=_VAULT_MOUNT
            )
            vault_val = ((resp.get("data") or {}).get("data") or {}).get(_VAULT_KEY)
            if vault_val and str(vault_val).strip():
                value = str(vault_val).strip()
        except hvac.exceptions.InvalidPath:
            value = None  # secret really absent → a cacheable "not configured"
    except Exception:
        # Read error (5xx, timeout, sealed): the key is UNKNOWN, not absent.
        # Only throttle the retry briefly so a Vault outage does not cost one
        # round trip per request — never hold "no key" for the full TTL.
        _key_cache["value"] = None
        _key_cache["expires"] = now + _KEY_ERROR_RETRY_SECONDS
        return None

    _key_cache["value"] = value
    _key_cache["expires"] = now + _KEY_CACHE_TTL_SECONDS
    return value


def system_api_key_configured() -> bool:
    """True when a non-empty system API key is available from env or Vault."""
    return resolve_system_api_key() is not None


def _resolve_effective(username: str) -> Optional[frozenset]:
    """Precompute the effective permission set for an identity builder.

    Returns ``None`` on failure so ``ApiIdentity.effective_permissions()`` can
    retry lazily instead of freezing an empty set into the identity.
    """
    try:
        from core.components.auth.logic import access_service

        return access_service.get_effective_permissions(username)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"API effective-permission precompute failed for '{username}': {exc}")
        return None


def _extract_bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def _try_api_key(request: Request) -> Optional[ApiIdentity]:
    """Validate the system API key from X-API-Key or a Bearer token."""
    # Short-circuit before touching Vault: only resolve the key when the request
    # actually carries a credential to compare against.
    presented = request.headers.get("X-API-Key") or _extract_bearer(request)
    if not presented:
        return None

    configured = resolve_system_api_key()
    if not configured:
        return None  # method disabled when no key is set

    if hmac.compare_digest(presented.strip(), configured):
        return ApiIdentity(
            method="api_key",
            username="system",
            roles=["admin", "superadmin"],
            is_system=True,
        )
    return None


def _try_user_api_key(request: Request) -> Optional[ApiIdentity]:
    """Validate a per-user API key (X-API-Key or Bearer) against the key store."""
    presented = request.headers.get("X-API-Key") or _extract_bearer(request)
    if not presented:
        return None

    try:
        from core.components.auth.logic.api_key_service import api_key_service

        resolved = api_key_service.verify(presented.strip())
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"API user-key check failed: {exc}")
        return None

    if resolved is None:
        return None

    roles: List[str] = []
    extra: List[str] = []
    try:
        from core.components.auth.logic.user_service import user_service

        owner = user_service.get_by_username(resolved.username)
        if owner is not None:
            roles = list(getattr(owner, "roles", None) or [])
            extra = list(getattr(owner, "extra_permissions", None) or [])
    except Exception:
        pass

    return ApiIdentity(
        method="user_api_key",
        username=resolved.username,
        roles=roles,
        extra_permissions=extra,
        permissions=_resolve_effective(resolved.username),
        key_scopes=resolved.scopes or None,
        key_id=resolved.id,
    )


def basic_auth_enabled() -> bool:
    """Whether HTTP Basic is accepted on API routes (``LYNDRIX_API_BASIC_AUTH``)."""
    try:
        from config import settings

        return bool(getattr(settings, "LYNDRIX_API_BASIC_AUTH", True))
    except Exception:  # pragma: no cover - defensive
        return True


def _try_basic_auth(request: Request) -> Optional[ApiIdentity]:
    """Validate HTTP Basic credentials against the local IAM.

    Every attempt is one throttled login attempt (``login_guard``). When it is
    not evaluated — too many failures, or no verification slot — this raises
    429/503 with ``Retry-After``: answering 401 would tell a correctly
    configured client that its password is wrong.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    if not basic_auth_enabled():
        return None

    try:
        raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None

    if ":" not in raw:
        return None
    username, password = raw.split(":", 1)

    from core.components.auth.logic.login_guard import LoginRejected, client_host

    try:
        from core.components.auth.logic.auth_service import auth_service

        user = auth_service.verify_local_login(username, password, client_host(request))
    except LoginRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"API basic-auth check failed: {exc}")
        return None

    if user is None:
        return None

    roles = list(getattr(user, "roles", None) or [])
    extra = list(getattr(user, "extra_permissions", None) or [])
    return ApiIdentity(
        method="basic",
        username=str(user.username),
        roles=roles,
        extra_permissions=extra,
        permissions=_resolve_effective(str(user.username)),
    )


def _try_session(request: Request) -> Optional[ApiIdentity]:
    """Accept an already-authenticated NiceGUI dashboard session, if reachable."""
    try:
        from core.session import get_user_value, is_authenticated

        if is_authenticated():
            username = str(get_user_value("username", "user"))
            roles = list(get_user_value("roles", []) or [])
            extra = list(get_user_value("extra_permissions", []) or [])
            return ApiIdentity(
                method="session",
                username=username,
                roles=roles,
                extra_permissions=extra,
                permissions=_resolve_effective(username),
            )
    except Exception:
        # No NiceGUI storage context for this request (typical for pure HTTP
        # clients) — simply skip this method.
        pass
    return None


def authenticate_request(request: Request, *, allow_basic: bool = True) -> Optional[ApiIdentity]:
    """
    Attempt every supported auth method and return the first identity found.

    Returns ``None`` when the request carries no valid credentials. A throttled
    HTTP Basic attempt raises ``HTTPException`` (429/503 + ``Retry-After``).

    ``allow_basic=False`` skips HTTP Basic: routes that also answer anonymous
    callers must not check passwords, or they become an oracle that tells
    "right" from "wrong" without ever answering 401.
    """
    # TODO(agent): re-add a session method here once React has a proper
    # JWT/cookie session that works outside the NiceGUI storage context.
    methods = [_try_api_key, _try_user_api_key]
    if allow_basic:
        methods.append(_try_basic_auth)
    for method in methods:
        identity = method(request)
        if identity is not None:
            return identity
    return None


def require_api_auth(request: Request) -> ApiIdentity:
    """
    FastAPI dependency: require a valid identity or raise ``401``.

    Use on protected endpoints::

        @app.post("/api/system/config")
        async def update(... , identity: ApiIdentity = Depends(require_api_auth)):
            ...
    """
    identity = authenticate_request(request)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            # Bearer only: advertising Basic makes browsers prompt for a
            # password and attach it to every later request to this origin.
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity


def optional_api_auth(request: Request) -> Optional[ApiIdentity]:
    """FastAPI dependency: return an identity if present, otherwise ``None``.

    API keys and session tokens only. HTTP Basic is deliberately ignored here
    (the caller is treated as anonymous): these routes never answer 401, so
    checking a password on them would be an unthrottled yes/no oracle.
    """
    return authenticate_request(request, allow_basic=False)


def require_permission(permission: str):
    """
    Build a FastAPI dependency that requires authentication *and* a specific
    permission from the Permissions system (e.g. ``"api:read"`` / ``"api:write"``).

    Authentication failures raise ``401``; authorization failures raise ``403``.

        @app.post("/api/system/config")
        async def update(... , identity: ApiIdentity = Depends(require_permission("api:write"))):
            ...
    """

    def dependency(request: Request) -> ApiIdentity:
        identity = require_api_auth(request)
        if not identity.allows(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
            )
        return identity

    return dependency


__all__ = [
    "ApiIdentity",
    "resolve_system_api_key",
    "invalidate_system_api_key_cache",
    "system_api_key_configured",
    "authenticate_request",
    "require_api_auth",
    "optional_api_auth",
    "require_permission",
]
