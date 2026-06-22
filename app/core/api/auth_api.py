from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from core.api.security import ApiIdentity, optional_api_auth, require_api_auth
from core.logger import get_logger

log = get_logger("Core:AuthAPI")

auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    roles: List[str]
    display_name: Optional[str]


@auth_router.post("/login", response_model=LoginResponse, summary="Obtain a session token")
async def login(payload: LoginRequest):
    """
    Authenticates via the configured provider chain (local / LDAP / OIDC).
    On success creates a named UserApiKey row and returns the raw key once.
    The React frontend stores it as a bearer token.
    """
    from core.components.auth.logic.auth_service import auth_service
    from core.components.auth.logic.api_key_service import api_key_service

    user = auth_service.authenticate_user(payload.username, payload.password)
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
