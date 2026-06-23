from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api.security import ApiIdentity, require_api_auth, require_permission
from core.logger import get_logger

log = get_logger("Core:UsersAPI")

users_router = APIRouter(prefix="/api/users", tags=["Users"])


class UserOut(BaseModel):
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    groups: List[str] = Field(default_factory=list)
    extra_permissions: List[str] = Field(default_factory=list)
    env_managed: bool = False


class UserCreateRequest(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    groups: List[str] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    roles: Optional[List[str]] = None
    groups: Optional[List[str]] = None


class ApiKeyCreateRequest(BaseModel):
    label: str = "API Key"
    scopes: List[str] = Field(default_factory=list)


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ApiKeyOut(BaseModel):
    id: int
    label: str
    prefix: str
    scopes: List[str]
    created_at: Optional[str]
    last_used_at: Optional[str]
    revoked: bool


def _user_service():
    from core.components.auth.logic.user_service import user_service
    return user_service


# Sync def on purpose: change_password runs Argon2, which is CPU-bound. A sync
# endpoint is executed in FastAPI's threadpool, keeping the hash off the event loop.
@users_router.post("/{username}/password", summary="Change a user's password")
def change_password(
    username: str,
    payload: PasswordChangeRequest,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Self-service for your own account; changing another user's password
    requires the ``api:write`` permission. The current password is always
    verified by the service before the change is applied."""
    if username != identity.username and not identity.allows("api:write"):
        raise HTTPException(status_code=403, detail="Cannot change another user's password")

    ok, msg = _user_service().change_password(
        username, payload.current_password, payload.new_password
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    log.info(f"API: Password changed for '{username}' by '{identity.username}'.")
    return {"status": "ok", "message": msg}


def _api_key_service():
    from core.components.auth.logic.api_key_service import api_key_service
    return api_key_service


def _db():
    from core.components.database.logic.db_service import db_instance
    return db_instance


def _to_user_out(user) -> UserOut:
    return UserOut(
        username=str(user.username),
        full_name=str(user.full_name or "") or None,
        email=str(user.email or "") or None,
        roles=list(user.roles or []),
        groups=list(user.groups or []),
        extra_permissions=list(user.extra_permissions or []),
        env_managed=_user_service().is_env_managed(str(user.username)),
    )


def _key_to_out(k) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        label=k.label,
        prefix=k.prefix,
        scopes=list(k.scopes or []),
        created_at=k.created_at.isoformat() if k.created_at else None,
        last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
        revoked=bool(k.revoked),
    )


# ── User CRUD ────────────────────────────────────────────────────────────────

@users_router.get("", summary="List all users")
async def list_users(identity: ApiIdentity = Depends(require_permission("api:read"))):
    users = [_to_user_out(u).model_dump() for u in _user_service().get_all()]
    return {"status": "ok", "count": len(users), "users": users}


@users_router.post("", summary="Create a user", status_code=201)
async def create_user(
    payload: UserCreateRequest,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    from core.components.auth.logic.models import User
    from core.components.auth.logic.hashing import hash_password

    svc = _user_service()
    uname = payload.username.strip()
    if not uname:
        raise HTTPException(status_code=422, detail="Username must not be empty")
    if svc.get_by_username(uname):
        raise HTTPException(status_code=409, detail=f"User '{uname}' already exists")
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    with _db().SessionLocal() as s:
        user = User(
            username=uname,
            hashed_password=hash_password(payload.password),
            full_name=(payload.full_name or "").strip() or None,
            email=(payload.email or "").strip() or None,
            roles=sorted(set(payload.roles)),
            groups=sorted(set(payload.groups)),
            extra_permissions=[],
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        out = _to_user_out(user)

    log.info(f"API: User '{uname}' created by '{identity.username}'.")
    return {"status": "ok", "user": out.model_dump()}


@users_router.get("/{username}", summary="Get a user")
async def get_user(
    username: str,
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    user = _user_service().get_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    return {"status": "ok", "user": _to_user_out(user).model_dump()}


@users_router.patch("/{username}", summary="Update a user")
async def update_user(
    username: str,
    payload: UserUpdateRequest,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    from core.components.auth.logic.models import User
    from core.components.auth.logic.hashing import hash_password

    if not _user_service().get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    if payload.password is not None and len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    with _db().SessionLocal() as s:
        user = s.query(User).filter(User.username == username).first()
        if payload.full_name is not None:
            user.full_name = payload.full_name.strip() or None
        if payload.email is not None:
            user.email = payload.email.strip() or None
        if payload.password is not None:
            user.hashed_password = hash_password(payload.password)
        if payload.roles is not None:
            user.roles = sorted(set(payload.roles))
        if payload.groups is not None:
            user.groups = sorted(set(payload.groups))
        s.commit()
        s.refresh(user)
        out = _to_user_out(user)

    log.info(f"API: User '{username}' updated by '{identity.username}'.")
    return {"status": "ok", "user": out.model_dump()}


@users_router.delete("/{username}", summary="Delete a user")
async def delete_user(
    username: str,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    if username == identity.username:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    from core.components.auth.logic.models import User

    if not _user_service().get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    with _db().SessionLocal() as s:
        user = s.query(User).filter(User.username == username).first()
        if user:
            s.delete(user)
            s.commit()

    log.info(f"API: User '{username}' deleted by '{identity.username}'.")
    return {"status": "ok", "deleted": username}


# ── Per-user API key management ─────────────────────────────────────────────

@users_router.get("/{username}/api-keys", summary="List API keys for a user")
async def list_api_keys(
    username: str,
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    if not _user_service().get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    keys = [_key_to_out(k).model_dump() for k in _api_key_service().list_for_user(username)]
    return {"status": "ok", "username": username, "api_keys": keys}


@users_router.post("/{username}/api-keys", summary="Create an API key for a user", status_code=201)
async def create_api_key(
    username: str,
    payload: ApiKeyCreateRequest,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    if not _user_service().get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    raw_key, info = _api_key_service().create(username, label=payload.label, scopes=payload.scopes)
    log.info(f"API: API key '{info.label}' created for '{username}' by '{identity.username}'.")
    return {
        "status": "ok",
        "username": username,
        "token": raw_key,
        "api_key": _key_to_out(info).model_dump(),
    }


@users_router.delete("/{username}/api-keys/{key_id}", summary="Revoke an API key")
async def revoke_api_key(
    username: str,
    key_id: int,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    if not _user_service().get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    ok = _api_key_service().delete(key_id, username=username)
    if not ok:
        raise HTTPException(status_code=404, detail=f"API key {key_id} not found for '{username}'")
    log.info(f"API: API key {key_id} deleted for '{username}' by '{identity.username}'.")
    return {"status": "ok", "deleted": key_id}
