import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import settings
from core.api.security import ApiIdentity, require_api_auth, require_permission
from core.logger import get_logger

log = get_logger("Core:AuthAPI")

auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])

# Per-user background images live on the filesystem (no DB change). Existence on
# disk is the source of truth. Served unauthenticated by username-in-path so the
# browser can load them via CSS url(...) (which cannot carry an auth header).
me_router = APIRouter(prefix="/api", tags=["User Background"])

_BG_MODES = {"dark", "light"}
_BG_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
_BG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_MAX_BG_BYTES = 8 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _user_backgrounds_base() -> Path:
    return Path(settings.STORAGE_DIR) / "user_backgrounds"


def _safe_username_component(username: str) -> str | None:
    """Sanitize a username into a single safe directory component, or None."""
    if not username:
        return None
    safe = _SAFE_NAME_RE.sub("_", username)
    safe = safe.strip(".")
    if not safe or safe in {".", ".."}:
        return None
    return safe


def _find_bg_file(username: str, mode: str) -> Path | None:
    """Return the existing background file for a user+mode, or None."""
    safe = _safe_username_component(username)
    if safe is None or mode not in _BG_MODES:
        return None
    user_dir = _user_backgrounds_base() / safe
    for ext in _BG_EXTS:
        candidate = user_dir / f"{mode}{ext}"
        if candidate.is_file():
            return candidate
    return None


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthConfigUpdate(BaseModel):
    updates: Dict[str, str]


# Vault keys whose values must never be returned in plaintext over the API.
_SECRET_CONFIG_KEYS = {"ldap_bind_password", "oidc_client_secret"}


class LoginResponse(BaseModel):
    token: str
    username: str
    roles: List[str]
    display_name: Optional[str]


def _describe_client(request: Request) -> str:
    """Best-effort 'Browser on OS' string from request headers.

    Substring checks over the User-Agent, most-specific first (Edge and Opera
    also contain 'Chrome/'). Brave deliberately sends a plain Chrome UA, so we
    additionally inspect the Sec-CH-UA client-hint brand list where it does
    identify itself. Unknowns degrade to 'Unknown client' — never an error.
    """
    ua = request.headers.get("user-agent", "")
    ch_brands = request.headers.get("sec-ch-ua", "").lower()

    if "brave" in ch_brands:
        browser = "Brave"
    elif "Edg/" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Unknown client"

    if "Windows NT" in ua:
        os_name = "Windows"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = ""

    return f"{browser} on {os_name}" if os_name else browser


@auth_router.post("/login", response_model=LoginResponse, summary="Obtain a session token")
async def login(payload: LoginRequest, request: Request):
    """
    Authenticates through the FULL provider chain (local, LDAP, … in
    LYNDRIX_AUTH_PROVIDERS order) including Identity-2.0 linking — external
    accounts resolve to their linked local profile. On success creates a named
    UserApiKey row and returns the raw key once; the React frontend stores it
    as a bearer token. Providers run credential checks off the event loop.
    """
    from core.components.auth.logic.providers.registry import provider_registry
    from core.components.auth.logic.api_key_service import api_key_service

    result = await provider_registry.authenticate(payload.username, payload.password)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Keep the 'web-session' prefix — the sessions UI distinguishes session
    # tokens from real API keys by exactly this marker. Label column is
    # String(100); this stays well under it.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    label = f"web-session · {_describe_client(request)} · {stamp} UTC"
    # The key insert is a sync DB write — keep it off the event loop.
    raw_token, _ = await asyncio.to_thread(
        api_key_service.create, result.username, label=label, scopes=[]
    )

    log.info(f"AUTH API: Login succeeded for '{result.username}' (provider={result.provider}).")
    return LoginResponse(
        token=raw_token,
        username=result.username,
        roles=list(result.roles or []),
        display_name=str(result.full_name or "") or None,
    )


# ==========================================
# Provider discovery + SSO flow for the React SPA (Identity 2.0)
# ==========================================

# One-time SSO exchange codes: the OIDC callback mints a session token
# server-side and hands the SPA a short-lived single-use code instead of
# putting the token itself in a redirect URL (referrer/history leak).
_SSO_EXCHANGE_TTL = 60.0
_sso_exchange: Dict[str, dict] = {}


def create_sso_exchange_code(payload: dict) -> str:
    """Store a login payload under a fresh one-time code (60s TTL)."""
    import secrets as _secrets
    import time as _time

    # opportunistic cleanup
    now = _time.monotonic()
    for k in [k for k, v in _sso_exchange.items() if v["expires"] < now]:
        _sso_exchange.pop(k, None)

    code = _secrets.token_urlsafe(32)
    _sso_exchange[code] = {"payload": payload, "expires": now + _SSO_EXCHANGE_TTL}
    return code


@auth_router.get("/providers", summary="Login-page provider discovery")
async def list_auth_providers():
    """UNAUTHENTICATED: metadata the login page needs BEFORE any login.

    Only provider ids/kinds/labels — no secrets, no config. ``credentials``
    providers share the username/password form; ``redirect`` providers render
    an SSO button pointing at /api/auth/oidc/start.
    """
    from core.components.auth.logic.providers.registry import provider_registry

    providers = []
    for p in provider_registry.get_form_providers():
        providers.append({"id": p.provider_id, "kind": "credentials", "label": p.display_name})
    for p in provider_registry.get_sso_providers():
        providers.append({"id": p.provider_id, "kind": "redirect", "label": p.display_name})
    if not providers:
        # Registry not initialized yet (early boot) — the local form always works.
        providers = [{"id": "local", "kind": "credentials", "label": "Local"}]
    return {"status": "ok", "providers": providers}


@auth_router.get("/oidc/start", summary="Begin the OIDC flow for the React SPA")
async def oidc_start_react(redirect_uri: str, next: str = "/"):
    """Redirect the browser to the IdP (PKCE + state bound to this flow).

    ``redirect_uri`` is the SPA's absolute /auth/callback URL; its origin MUST
    be in CORS_ALLOWED_ORIGINS (open-redirect protection). ``next`` is the
    in-app path to land on after the exchange.
    """
    from urllib.parse import urlparse
    from fastapi.responses import RedirectResponse
    from config import settings
    from core.components.auth.logic.providers.registry import provider_registry

    parsed = urlparse(redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in set(settings.CORS_ALLOWED_ORIGINS):
        raise HTTPException(status_code=400, detail="redirect_uri origin not allowed")
    if not next.startswith("/"):
        next = "/"

    provider = provider_registry.get("oidc")
    if provider is None or not provider.is_configured():
        raise HTTPException(status_code=404, detail="No OIDC provider configured")

    url = await provider.build_login_url(
        flow="react", redirect=f"{redirect_uri}|{next}"
    )
    if not url:
        raise HTTPException(status_code=503, detail="OIDC provider unavailable")
    return RedirectResponse(url, status_code=302)


class TokenExchangeRequest(BaseModel):
    code: str


@auth_router.post("/token-exchange", response_model=LoginResponse, summary="Redeem a one-time SSO code")
async def sso_token_exchange(payload: TokenExchangeRequest):
    """Swap the single-use code from the SSO callback for the session token."""
    import time as _time

    entry = _sso_exchange.pop(payload.code, None)
    if entry is None or entry["expires"] < _time.monotonic():
        raise HTTPException(status_code=401, detail="Invalid or expired exchange code")
    data = entry["payload"]
    return LoginResponse(
        token=data["token"],
        username=data["username"],
        roles=list(data.get("roles") or []),
        display_name=data.get("display_name"),
    )


@auth_router.post("/logout", summary="Revoke the current session token")
def logout(request: Request, identity: ApiIdentity = Depends(require_api_auth)):
    """Deletes the caller's UserApiKey row — immediate, irrevocable revocation.

    Sync (threadpool): the api_key verify/delete are blocking DB calls, so this
    runs in FastAPI's threadpool rather than on the event loop.
    """
    from core.components.auth.logic.api_key_service import api_key_service

    raw_key: Optional[str] = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw_key = auth_header[7:].strip()
    else:
        raw_key = request.headers.get("X-API-Key", "").strip() or None

    if raw_key and raw_key.startswith("lyk_"):
        resolved = api_key_service.verify(raw_key)
        if resolved:
            api_key_service.delete(resolved.id, username=identity.username)
            log.info(f"AUTH API: Session key revoked for '{identity.username}'.")

    return {"status": "ok", "message": "Logged out"}


def _serialize_auth_field(spec, value: str, source: str) -> Dict[str, object]:
    """Serialize an auth FieldSpec + its resolved (value, source) for the API.

    Mirrors the notification provider-config shape: per-field metadata plus the
    effective ``source`` (env / vault / default). Sensitive values are never
    returned — a ``configured`` flag tells the UI a secret is set, and a blank
    sensitive value on PATCH means "keep the stored one".
    """
    is_env_locked = source == "env"
    return {
        "vault_key": spec.vault_key,
        "label": spec.label,
        "hint": spec.hint,
        "env_var": spec.env_var,
        "sensitive": spec.sensitive,
        "is_bool": spec.is_bool,
        "source": source,
        "is_env_locked": is_env_locked,
        "configured": bool(value),
        "current_value": "" if spec.sensitive else value,
    }


@auth_router.get("/config", summary="Get auth provider configuration (secrets masked)")
def get_auth_config(identity: ApiIdentity = Depends(require_permission("api:read"))):
    """Return the auth provider configuration as a metadata-rich ``fields`` array.

    Each field reports its label/hint/env-var, the effective value + its source
    (env → vault → default), and whether it is env-locked (cannot be edited in the
    UI). Secret values are masked (``current_value=""`` + ``configured=true``).
    The legacy ``config`` map (masked) is kept for backwards compatibility."""
    from core.components.auth.logic.auth_config import auth_config_service, ALL_SPECS

    effective = auth_config_service.get_all_effective()
    fields = [
        _serialize_auth_field(spec, *effective[spec.vault_key])
        for spec in ALL_SPECS
    ]
    # Legacy masked map (raw Vault values) for any older consumer.
    data = auth_config_service.load_vault_data()
    config = {
        k: ("********" if (k in _SECRET_CONFIG_KEYS and v) else v)
        for k, v in data.items()
    }
    return {"status": "ok", "fields": fields, "config": config}


@auth_router.patch("/config", summary="Update auth provider configuration")
def update_auth_config(
    payload: AuthConfigUpdate,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    """Persist auth provider config to Vault (core/auth) and reinitialize the
    provider chain so changes take effect immediately.

    Fields whose value comes from an OS environment variable are env-locked and
    rejected up-front with HTTP 409 (don't half-apply). Blank values for sensitive
    fields are skipped (keep the stored secret); blank non-sensitive values clear
    the stored key (same semantics as the NiceGUI page)."""
    from core.components.auth.logic.auth_config import auth_config_service, _SPEC_MAP

    effective = auth_config_service.get_all_effective()

    # Reject up-front if any requested field is env-locked.
    locked = [
        k for k in payload.updates
        if k in effective and effective[k][1] == "env"
    ]
    if locked:
        raise HTTPException(
            status_code=409,
            detail={"error": "env_locked", "locked_fields": sorted(locked)},
        )

    # Skip blank sensitive values so a masked secret is kept, not deleted.
    updates = {}
    skipped: List[str] = []
    for key, value in payload.updates.items():
        spec = _SPEC_MAP.get(key)
        if spec and spec.sensitive and not str(value).strip():
            skipped.append(key)
            continue
        updates[key] = value

    from core.components.auth.logic.auth_service import auth_service
    try:
        auth_config_service.save_vault_data(updates)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    auth_service.reinitialize_providers()
    log.info(f"AUTH API: provider config updated by '{identity.username}'.")
    return {"status": "ok", "updated_keys": sorted(updates.keys()), "skipped": sorted(skipped)}


@auth_router.post("/reload", summary="Reinitialize the auth provider chain")
def reload_auth_providers(identity: ApiIdentity = Depends(require_permission("api:write"))):
    """Re-read provider configuration and rebuild the auth chain (local/LDAP/OIDC)."""
    from core.components.auth.logic.auth_service import auth_service

    auth_service.reinitialize_providers()
    log.info(f"AUTH API: providers reloaded by '{identity.username}'.")
    return {"status": "ok", "reloaded": True}


@auth_router.get("/me", summary="Current user profile")
def me(identity: ApiIdentity = Depends(require_api_auth)):
    """Returns full profile for the authenticated user (sync DB read → threadpool)."""
    from core.components.auth.logic.user_service import user_service

    user = user_service.get_by_username(identity.username)
    if user is None:
        return {
            "username": identity.username,
            "full_name": None,
            "email": None,
            "roles": identity.roles,
            "groups": [],
            "extra_permissions": identity.extra_permissions,
            "method": identity.method,
            "is_system": identity.is_system,
        }
    return {
        "username": str(user.username),
        "full_name": str(user.full_name or "") or None,
        "email": str(user.email or "") or None,
        "roles": list(user.roles or []),
        "groups": list(user.groups or []),
        "extra_permissions": list(user.extra_permissions or []),
        "method": identity.method,
        "is_system": identity.is_system,
    }


@me_router.get("/me/access", summary="Effective access view for the caller")
def my_access(identity: ApiIdentity = Depends(require_api_auth)):
    """Server-derived effective-permissions view (Identity & Permissions 2.0).

    The React shell consumes this to gate the sidebar, routes and dashboard
    sections — it never re-implements permission matching client-side. The
    ``plugins`` map is derived from the registry + loaded manifests:
    ``visible``/``can_read``/``can_write`` honour the per-plugin fallback
    (global api:read/write) and ``routes`` lists the react_routes the caller
    may open. Sync ``def`` (threadpool): blocking DB reads.
    """
    from core.components.auth.logic import access_service
    from core.components.auth.logic.user_service import user_service

    is_superadmin = identity.is_system or ("superadmin" in identity.roles)
    effective = access_service.get_effective_permissions(identity.username)

    user = user_service.get_by_username(identity.username)
    groups = list(getattr(user, "groups", None) or []) if user else []

    def _allowed(perm: str) -> bool:
        return is_superadmin or access_service.has_permission(effective, perm)

    plugins: Dict[str, dict] = {}
    try:
        from core.components.plugins.logic.manager import module_manager

        for manifest in module_manager.get_manifests():
            if getattr(manifest, "type", "PLUGIN") != "PLUGIN":
                continue
            pid = manifest.id
            visible = _allowed(f"plugin:{pid}")
            routes = []
            for rr in getattr(manifest, "react_routes", None) or []:
                path = (rr or {}).get("path")
                if not path:
                    continue
                # plugin:<id> is the umbrella grant (all routes); a single
                # plugin:<id>:route:<path> grant opens one route without
                # whole-plugin visibility.
                if is_superadmin or visible or _allowed(f"plugin:{pid}:route:{path}"):
                    routes.append(path)
            plugins[pid] = {
                "visible": visible,
                "can_read": _allowed(f"plugin:{pid}:api:read"),
                "can_write": _allowed(f"plugin:{pid}:api:write"),
                "routes": routes,
            }
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"AUTH API: /me/access plugin derivation failed: {exc}")

    return {
        "status": "ok",
        "username": identity.username,
        "groups": groups,
        "is_superadmin": is_superadmin,
        "permissions": sorted(effective),
        "plugins": plugins,
    }


# ==========================================
# Per-user preferences ("My account" scope — Theming v2 Phase 2)
# ==========================================
class PreferencesUpdate(BaseModel):
    """Partial, namespaced preferences patch. Deep-merged into the stored document."""
    preferences: Dict[str, object]


@me_router.get("/me/preferences", summary="Get the caller's account preferences")
def get_my_preferences(identity: ApiIdentity = Depends(require_api_auth)):
    """Return the authenticated user's namespaced preferences document.

    Sync ``def`` (threadpool): the read is a blocking DB query, so it runs in
    FastAPI's threadpool rather than on the shared event loop. Keyed strictly by
    ``identity.username`` — a user only ever sees their own preferences.
    """
    from core.components.auth.logic.preferences_service import preferences_service

    return {"preferences": preferences_service.get(identity.username)}


@me_router.put("/me/preferences", summary="Deep-merge a patch into the caller's preferences")
def update_my_preferences(
    payload: PreferencesUpdate,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Deep-merge ``payload.preferences`` into the caller's stored document.

    Nested objects merge recursively; scalars/lists replace. Returns the full
    merged document. Sync ``def`` (threadpool) — the merge is a blocking DB
    read-modify-write. Keyed by ``identity.username`` only.
    """
    from core.components.auth.logic.preferences_service import preferences_service

    merged = preferences_service.merge(identity.username, payload.preferences)
    log.info(f"AUTH API: preferences updated for '{identity.username}'.")
    return {"preferences": merged}


@me_router.delete("/me/preferences/{dotted_key}", summary="Remove one preference path")
def delete_my_preference(
    dotted_key: str,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Remove a single dotted path (e.g. ``theme.selection.react``) from the caller's
    document and return the remaining preferences. Sync ``def`` (threadpool)."""
    from core.components.auth.logic.preferences_service import preferences_service

    remaining = preferences_service.delete_path(identity.username, dotted_key)
    return {"preferences": remaining}


# ==========================================
# Per-user background images (filesystem; no DB change)
# ==========================================
def _bg_url(username: str, mode: str, path: Path) -> str:
    safe = _safe_username_component(username) or username
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"/api/users/{safe}/background/{mode}?v={version}"


@me_router.get("/me/background", summary="List the caller's background image URLs")
async def get_my_background(identity: ApiIdentity = Depends(require_api_auth)):
    """Return ``{light, dark}`` URLs for whichever per-user images exist.

    URLs point at the unauthenticated ``/api/users/{username}/background/{mode}``
    route (loadable from CSS) and carry ``?v=<mtime>`` for cache-busting.
    """
    result: Dict[str, Optional[str]] = {"light": None, "dark": None}
    for mode in ("light", "dark"):
        existing = _find_bg_file(identity.username, mode)
        if existing is not None:
            result[mode] = _bg_url(identity.username, mode, existing)
    return result


@me_router.post("/me/background", summary="Upload the caller's background image")
async def upload_my_background(
    mode: str = Query(...),
    file: UploadFile = File(...),
    identity: ApiIdentity = Depends(require_api_auth),
):
    if mode not in _BG_MODES:
        raise HTTPException(status_code=422, detail="mode must be 'dark' or 'light'")

    ext = _BG_CONTENT_TYPES.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(
            status_code=422, detail="Unsupported content type (allowed: png, jpeg, webp)"
        )

    safe = _safe_username_component(identity.username)
    if safe is None:
        raise HTTPException(status_code=400, detail="Invalid username")

    data = await file.read()
    if len(data) > _MAX_BG_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 8 MB limit")
    if not data:
        raise HTTPException(status_code=422, detail="Empty upload")

    user_dir = _user_backgrounds_base() / safe
    user_dir.mkdir(parents=True, exist_ok=True)

    # Remove any prior file for this mode with a different extension.
    for prior_ext in _BG_EXTS:
        prior = user_dir / f"{mode}{prior_ext}"
        if prior_ext != ext and prior.exists():
            try:
                prior.unlink()
            except OSError:
                pass

    target = user_dir / f"{mode}{ext}"
    target.write_bytes(data)

    log.info(f"AUTH API: background ({mode}) uploaded for '{identity.username}'.")
    return {"status": "ok", "mode": mode, "url": _bg_url(identity.username, mode, target)}


@me_router.delete("/me/background/{mode}", summary="Delete the caller's background image")
async def delete_my_background(
    mode: str,
    identity: ApiIdentity = Depends(require_api_auth),
):
    if mode not in _BG_MODES:
        raise HTTPException(status_code=422, detail="mode must be 'dark' or 'light'")

    existing = _find_bg_file(identity.username, mode)
    if existing is not None:
        try:
            existing.unlink()
        except OSError:
            pass
    return {"status": "ok", "mode": mode, "deleted": existing is not None}


# Unauthenticated on purpose: the browser loads this via CSS url(...) which
# cannot carry an Authorization header. The username is in the path; background
# images are not secret, and only files that exist on disk are served.
@me_router.get("/users/{username}/background/{mode}", summary="Serve a user's background image")
async def get_user_background(username: str, mode: str):
    if mode not in _BG_MODES:
        raise HTTPException(status_code=404, detail="Not found")
    existing = _find_bg_file(username, mode)
    if existing is None:
        raise HTTPException(status_code=404, detail="Not found")

    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(existing.suffix.lower(), "application/octet-stream")

    # Short cache + ?v=mtime busting: the file changes on re-upload.
    return FileResponse(
        existing,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=300, must-revalidate"},
    )
