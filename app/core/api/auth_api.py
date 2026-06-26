import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from core.api.security import ApiIdentity, require_api_auth, require_permission
from core.logger import get_logger

log = get_logger("Core:AuthAPI")

auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])


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


@auth_router.post("/login", response_model=LoginResponse, summary="Obtain a session token")
async def login(payload: LoginRequest):
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

    label = f"web-session-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    raw_token, _ = api_key_service.create(str(user.username), label=label, scopes=[])

    log.info(f"AUTH API: Login succeeded for '{user.username}'.")
    return LoginResponse(
        token=raw_token,
        username=str(user.username),
        roles=list(user.roles or []),
        display_name=str(user.full_name or "") or None,
    )


@auth_router.post("/logout", summary="Revoke the current session token")
async def logout(request: Request, identity: ApiIdentity = Depends(require_api_auth)):
    """Deletes the caller's UserApiKey row — immediate, irrevocable revocation."""
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
async def get_auth_config(identity: ApiIdentity = Depends(require_permission("api:read"))):
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
async def update_auth_config(
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
async def reload_auth_providers(identity: ApiIdentity = Depends(require_permission("api:write"))):
    """Re-read provider configuration and rebuild the auth chain (local/LDAP/OIDC)."""
    from core.components.auth.logic.auth_service import auth_service

    auth_service.reinitialize_providers()
    log.info(f"AUTH API: providers reloaded by '{identity.username}'.")
    return {"status": "ok", "reloaded": True}


@auth_router.get("/me", summary="Current user profile")
async def me(identity: ApiIdentity = Depends(require_api_auth)):
    """Returns full profile for the authenticated user."""
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
