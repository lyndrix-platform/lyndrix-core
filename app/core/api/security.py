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
  2. HTTP Basic       — ``Authorization: Basic <base64(user:pass)>`` validated
                         against the local IAM via ``auth_service.authenticate_user``.
  3. Session cookie   — an already-authenticated NiceGUI dashboard session
                         (``core.session.is_authenticated``), when the request is
                         processed inside NiceGUI's storage context.

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
_key_cache: dict = {"value": None, "expires": 0.0}


def invalidate_system_api_key_cache() -> None:
    """Drop the cached Vault key so the next resolve re-reads from Vault.

    Call after the system API key is changed via the settings UI/API.
    TODO(agent): wire this into the settings-write path in
    core/components/settings (out of this unit's scope) so the change is
    instant rather than bounded by the cache TTL.
    """
    _key_cache["value"] = None
    _key_cache["expires"] = 0.0


@dataclass
class ApiIdentity:
    """Resolved identity for an authenticated API request."""

    method: str                       # "api_key" | "user_api_key" | "basic" | "session"
    username: str = "system"
    roles: List[str] = field(default_factory=list)
    extra_permissions: List[str] = field(default_factory=list)
    is_system: bool = False           # True for the master system API key
    # When set (non-None), the credential restricts access to this subset of
    # permissions (used by per-user API keys with explicit scopes).
    key_scopes: Optional[List[str]] = None

    def allows(self, permission: str) -> bool:
        """
        Return True if this identity is authorized for ``permission``.

        - The master system key bypasses all permission checks.
        - The ``superadmin`` role bypasses permission checks (matches UI convention).
        - Otherwise the owner's effective permissions are resolved via the
          group/permission system (Permissions tab).
        - A scoped per-user key additionally requires the permission to be within
          its ``key_scopes``.
        """
        if self.is_system:
            return True

        if "superadmin" in self.roles:
            base_allowed = True
        else:
            try:
                from core.components.auth.logic.group_service import group_service

                base_allowed = group_service.user_has_permission(
                    self.roles, permission, self.extra_permissions
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(f"API authz check failed for '{permission}': {exc}")
                base_allowed = False

        if self.key_scopes is not None:
            return base_allowed and (permission in self.key_scopes)
        return base_allowed


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
        from core.services import vault_instance

        if vault_instance.is_connected:
            resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                path=_VAULT_PATH, mount_point=_VAULT_MOUNT
            )
            vault_val = (resp.get("data", {}).get("data", {}) or {}).get(_VAULT_KEY)
            if vault_val and str(vault_val).strip():
                value = str(vault_val).strip()
    except Exception:
        # Vault unavailable / key absent — fall through to "not configured".
        value = None

    _key_cache["value"] = value
    _key_cache["expires"] = now + _KEY_CACHE_TTL_SECONDS
    return value


def system_api_key_configured() -> bool:
    """True when a non-empty system API key is available from env or Vault."""
    return resolve_system_api_key() is not None


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
        key_scopes=resolved.scopes or None,
    )


def _try_basic_auth(request: Request) -> Optional[ApiIdentity]:
    """Validate HTTP Basic credentials against the local IAM."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("basic "):
        return None

    try:
        raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None

    if ":" not in raw:
        return None
    username, password = raw.split(":", 1)

    try:
        from core.components.auth.logic.auth_service import auth_service

        user = auth_service.authenticate_user(username, password)
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
            )
    except Exception:
        # No NiceGUI storage context for this request (typical for pure HTTP
        # clients) — simply skip this method.
        pass
    return None


def authenticate_request(request: Request) -> Optional[ApiIdentity]:
    """
    Attempt every supported auth method and return the first identity found.

    Returns ``None`` when the request carries no valid credentials.
    """
    for method in (_try_api_key, _try_user_api_key, _try_basic_auth, _try_session):
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
            headers={"WWW-Authenticate": 'Bearer, Basic realm="Lyndrix"'},
        )
    return identity


def optional_api_auth(request: Request) -> Optional[ApiIdentity]:
    """FastAPI dependency: return an identity if present, otherwise ``None``."""
    return authenticate_request(request)


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
