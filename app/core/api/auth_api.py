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
    Authenticates against the local user database (Argon2). On success creates a
    named UserApiKey row and returns the raw key once; the React frontend stores it
    as a bearer token. Credential verification runs off the event loop (Argon2 is
    CPU-bound) so concurrent requests are not stalled.
    """
    from core.components.auth.logic.auth_service import auth_service
    from core.components.auth.logic.api_key_service import api_key_service

    # Argon2 verify + the sync DB session are blocking; keep them off the event loop.
    user = await asyncio.to_thread(
        auth_service.authenticate_user, payload.username, payload.password
    )
    if user is None:
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
        api_key_service.create, str(user.username), label=label, scopes=[]
    )

    log.info(f"AUTH API: Login succeeded for '{user.username}'.")
    return LoginResponse(
        token=raw_token,
        username=str(user.username),
        roles=list(user.roles or []),
        display_name=str(user.full_name or "") or None,
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
