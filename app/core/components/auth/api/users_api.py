import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.api.security import ApiIdentity, require_api_auth, require_permission
from core.components.auth.logic.models import email_is_trusted
from core.logger import get_logger

log = get_logger("Core:UsersAPI")

users_router = APIRouter(prefix="/api/users", tags=["Users"])

# System role flags assignable through the user API. These are NEVER resolved
# to permissions directly (see access_service) — "superadmin" is the
# break-glass bypass in ApiIdentity.allows(), "bot" is informational. Only a
# caller who is already superadmin may grant either, and only these two
# values are accepted — this is the privilege-escalation guard for
# create/update (a client can never hand itself e.g. "roles": ["anything"]).
_ASSIGNABLE_ROLES = {"superadmin", "bot"}


def _is_superadmin(identity: ApiIdentity) -> bool:
    # Never a scoped key: its scopes bound it, whoever the owner is.
    return identity.is_superadmin


def _require_unscoped(identity: ApiIdentity, action: str) -> None:
    """Credential management needs a full credential (AZ-04).

    A scope-restricted API key is a deliberately limited credential. If it
    could mint a key, set a password or change the email it would simply grant
    itself the owner's full power — scopes would bound nothing.
    """
    if identity.is_scoped:
        raise HTTPException(
            status_code=403,
            detail=f"A scope-restricted API key cannot {action}; use a session or an unscoped key",
        )


def _require_superadmin_for(target, identity: ApiIdentity, action: str) -> None:
    """Credentials of a superadmin account are superadmin business (AZ-07).

    ``superadmin`` is the one unbounded tier. An ``admin:write`` holder who could
    mint a key for such an account, reset its password, point its email (the
    identity auto-link target) somewhere else or delete it would BE that tier.
    ``target`` is the stored user row; a superadmin acting on their own account
    passes because they are one.
    """
    if "superadmin" in (getattr(target, "roles", None) or []) and not _is_superadmin(identity):
        raise HTTPException(
            status_code=403,
            detail=f"Only a superadmin may {action} of a superadmin account",
        )


def _require_local_password(target, identity: ApiIdentity, is_self: bool) -> None:
    """An account without a local password cannot give itself one (AZ-05).

    SSO/LDAP-only profiles are meant to be governed by the directory: a local
    password set through self-service would keep working after the account is
    disabled there, and sidestep the provider's MFA. An administrator may
    still set one deliberately.
    """
    if is_self and not getattr(target, "hashed_password", None) and not identity.is_system:
        raise HTTPException(
            status_code=403,
            detail=(
                "This account signs in through an external provider and has no local "
                "password; ask an administrator to set one"
            ),
        )


def _revoke_other_sessions(username: str, identity: ApiIdentity) -> int:
    """After a password change: sign out the account's login sessions, except
    the one that made the change. Named API keys are deliberately kept."""
    keep = identity.key_id if _same_username(username, identity.username) else None
    try:
        return _api_key_service().revoke_sessions(username, except_key_id=keep)
    except Exception as exc:  # the password IS changed — report, do not fail the request
        log.error(f"API: Could not revoke sessions of '{username}' after a password change: {exc}")
        return 0


def _registry_role_exists(role_id: str) -> bool:
    """A plugin/core-declared role (core 1.8: assignable directly on a user)."""
    try:
        from core.components.auth.logic.role_registry import role_registry

        role_registry.scan_from_manifests()
        return role_registry.get(role_id) is not None
    except Exception:
        return False


def _same_username(a: Optional[str], b: Optional[str]) -> bool:
    """Case-insensitive username equality.

    Usernames are looked up case-insensitively (MariaDB default collation), so
    a raw ``==`` on the path parameter would let ``PATCH /api/users/ADMIN`` by
    caller ``admin`` read the caller's own row yet evaluate ``is_self`` False —
    slipping into the admin branch and self-editing. Always compare the
    CANONICAL stored username (``User.username``) casefolded, never the raw
    path segment.
    """
    return (a or "").casefold() == (b or "").casefold()


def _list_changed(new_values: Optional[List[str]], current_values: Optional[List[str]]) -> bool:
    """True if `new_values` is present in the payload AND differs from the
    persisted value (compared as sets, so re-sending the same membership in a
    different order — e.g. an echoed field — is never treated as a change)."""
    if new_values is None:
        return False
    return sorted(set(new_values)) != sorted(set(current_values or []))


class UserOut(BaseModel):
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    # False = the owner typed the address in themselves: it is shown, but never
    # used to auto-link an LDAP/OIDC identity to this account.
    email_trusted: bool = False
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
    # Required when a user changes their OWN password through PATCH (an
    # administrator resetting somebody else's does not need it).
    current_password: Optional[str] = None
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
    request: Request,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Self-service for your own account; changing another user's password
    requires the ``admin:write`` permission. The current password is always
    verified by the service before the change is applied — as a throttled
    login attempt, so a wrong one answers 400 and too many wrong ones 429."""
    from core.components.auth.logic.login_guard import LoginRejected, client_host

    _require_unscoped(identity, "change a password")
    if username != identity.username and not identity.allows("admin:write"):
        raise HTTPException(status_code=403, detail="Cannot change another user's password")
    target = _user_service().get_by_username(username)
    if target is not None:
        _require_superadmin_for(target, identity, "change the password")
        _require_local_password(target, identity, _same_username(target.username, identity.username))

    try:
        ok, msg = _user_service().change_password(
            username, payload.current_password, payload.new_password,
            client_ip=client_host(request), raise_rejected=True,
        )
    except LoginRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    revoked = _revoke_other_sessions(str(target.username) if target is not None else username, identity)
    log.info(f"API: Password changed for '{username}' by '{identity.username}'.")
    return {"status": "ok", "message": msg, "revoked_sessions": revoked}


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
        email_trusted=email_is_trusted(user),
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
def list_users(identity: ApiIdentity = Depends(require_permission("admin:read"))):
    # Sync (threadpool): get_all() is a blocking DB read.
    users = [_to_user_out(u).model_dump() for u in _user_service().get_all()]
    return {"status": "ok", "count": len(users), "users": users}


@users_router.post("", summary="Create a user", status_code=201)
async def create_user(
    payload: UserCreateRequest,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    from core.components.auth.logic.models import User
    from core.components.auth.logic.hashing import hash_password

    svc = _user_service()
    uname = payload.username.strip()
    if not uname:
        raise HTTPException(status_code=422, detail="Username must not be empty")
    if await asyncio.to_thread(svc.get_by_username, uname):
        raise HTTPException(status_code=409, detail=f"User '{uname}' already exists")
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    # System role flags are the break-glass bypass — only a superadmin may hand
    # them out, and only the two known values are accepted at all.
    requested_roles = sorted(set(payload.roles or []))
    if requested_roles:
        unknown = [r for r in requested_roles if r not in _ASSIGNABLE_ROLES and not _registry_role_exists(r)]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown role(s): {', '.join(unknown)}")
        # Registry roles (plugin bundles) need admin:write only; the system flags
        # stay a superadmin privilege.
        if (set(requested_roles) & _ASSIGNABLE_ROLES) and not _is_superadmin(identity):
            raise HTTPException(status_code=403, detail="Only a superadmin may assign system roles at creation")

    # Argon2 is CPU/memory-heavy; run it off the event loop so a user create
    # never blocks every other client (same rule as change_password).
    hashed = await asyncio.to_thread(hash_password, payload.password)

    new_email = (payload.email or "").strip() or None

    def _persist():
        with _db().SessionLocal() as s:
            user = User(
                username=uname,
                hashed_password=hashed,
                full_name=(payload.full_name or "").strip() or None,
                email=new_email,
                email_trusted=bool(new_email),  # set by an administrator
                roles=requested_roles,
                groups=sorted(set(payload.groups)),
                extra_permissions=[],
            )
            # A new account never inherits keys/preferences an earlier account
            # of this name left behind.
            _api_key_service().purge_for_username(s, uname)
            s.add(user)
            s.commit()
            s.refresh(user)
            return _to_user_out(user)

    # The DB insert is blocking pymysql — keep it off the event loop too.
    out = await asyncio.to_thread(_persist)

    log.info(f"API: User '{uname}' created by '{identity.username}'.")
    return {"status": "ok", "user": out.model_dump()}


@users_router.get("/{username}", summary="Get a user")
def get_user(
    username: str,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Self-read needs no special permission; reading another user's profile
    requires ``admin:read``."""
    if username != identity.username and not identity.allows("admin:read"):
        raise HTTPException(status_code=403, detail="Missing required permission: admin:read")
    # Sync (threadpool): get_by_username() is a blocking DB read.
    user = _user_service().get_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    return {"status": "ok", "user": _to_user_out(user).model_dump()}


@users_router.patch("/{username}", summary="Update a user")
async def update_user(
    username: str,
    payload: UserUpdateRequest,
    request: Request,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Self-service for full_name/email/password (the React dashboard lets any
    authenticated user edit their own profile); everything else needs
    ``admin:write``, and role changes additionally require the caller to be
    superadmin. "Changed" means the field is present in the payload AND
    differs from the stored value — echoing back an unchanged value never
    403s.

    Changing your OWN password needs ``current_password`` (422 without it, 400
    when it is wrong, 429 after too many wrong ones) — a stolen session must
    not be enough to take the account over. A scope-restricted API key can
    change neither password nor email (403)."""
    from core.components.auth.logic.models import User
    from core.components.auth.logic.hashing import hash_password

    # Authorization BEFORE the lookup: without admin rights the answer for any
    # other name is 403, whether or not such a user exists (404 here would let
    # every account enumerate usernames).
    if not _same_username(username, identity.username) and not identity.allows("admin:write"):
        raise HTTPException(status_code=403, detail="Missing required permission: admin:write")

    current = await asyncio.to_thread(_user_service().get_by_username, username)
    if not current:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    # Compare the CANONICAL stored username, not the raw path segment — the
    # lookup is case-insensitive, so a differently-cased path must still be
    # recognised as self (else it slips into the admin branch).
    is_self = _same_username(current.username, identity.username)
    roles_changed = _list_changed(payload.roles, current.roles)
    groups_changed = _list_changed(payload.groups, current.groups)

    if is_self:
        # Self-service never touches privileged fields, even for an admin
        # editing their own account — group/role escalation must go through
        # another admin acting on the target account.
        if roles_changed or groups_changed:
            raise HTTPException(
                status_code=403,
                detail="Cannot change your own roles or group membership",
            )
    elif not identity.allows("admin:write"):
        raise HTTPException(status_code=403, detail="Missing required permission: admin:write")

    if roles_changed:
        # Core 1.8: User.roles carries the system flags AND registry roles
        # (plugin-declared bundles). Flags stay superadmin-only; registry roles
        # need admin:write (checked above) and must exist in the registry.
        requested = set(payload.roles or [])
        current_flags = set(current.roles or []) & _ASSIGNABLE_ROLES
        if (requested & _ASSIGNABLE_ROLES) != current_flags and not _is_superadmin(identity):
            raise HTTPException(status_code=403, detail="Only a superadmin may change system roles")
        unknown = [r for r in requested if r not in _ASSIGNABLE_ROLES and not _registry_role_exists(r)]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"Unknown role(s): {', '.join(sorted(set(unknown)))}"
            )

    if payload.password is not None and len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    email_changes = (
        payload.email is not None
        and payload.email.strip().casefold() != (current.email or "").strip().casefold()
    )
    if payload.password is not None:
        _require_unscoped(identity, "change a password")
        _require_superadmin_for(current, identity, "reset the password")
    if email_changes:
        _require_unscoped(identity, "change an email address")
        # The email is what an LDAP/OIDC identity is auto-linked by.
        _require_superadmin_for(current, identity, "change the email")

    if payload.password is not None:
        _require_local_password(current, identity, is_self)

    if is_self and payload.password is not None:
        # Self-service: prove knowledge of the current password, as the
        # dedicated POST /{username}/password endpoint always did. Runs as a
        # throttled login attempt off the event loop.
        if not payload.current_password:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Changing your own password requires your current password "
                    "(field 'current_password', or POST /api/users/{username}/password — "
                    "in the app: Settings → Profile → Password)"
                ),
            )
        from core.components.auth.logic.auth_service import auth_service
        from core.components.auth.logic.login_guard import LoginRejected, client_host

        try:
            confirmed = await asyncio.to_thread(
                auth_service.verify_local_login,
                str(current.username), payload.current_password, client_host(request),
            )
        except LoginRejected as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        if confirmed is None:
            # 400, not 401: the session is fine, only the confirmation failed.
            raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Hash off the event loop before opening the DB session (Argon2 is heavy).
    hashed = (
        await asyncio.to_thread(hash_password, payload.password)
        if payload.password is not None
        else None
    )
    # An administrator may already set any other account's email, so their own
    # counts as an admin edit too; everyone else's own edit is self-service.
    email_set_by_admin = identity.allows("admin:write")

    def _persist():
        with _db().SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if user is None:
                # Deleted between the existence check and this write.
                return None
            if payload.full_name is not None:
                user.full_name = payload.full_name.strip() or None
            if payload.email is not None:
                new_email = payload.email.strip() or None
                # Echoing the stored address back (the profile form always
                # sends it) is not a change and keeps its provenance.
                if (new_email or "").casefold() != (user.email or "").strip().casefold():
                    # Trusted only when set with administrative authority; an
                    # address the owner types in is unverified and must never
                    # become a target for identity auto-linking (AZ-02).
                    user.email_trusted = bool(new_email) and email_set_by_admin
                user.email = new_email
            if hashed is not None:
                user.hashed_password = hashed
            if payload.roles is not None:
                user.roles = sorted(set(payload.roles))
            if payload.groups is not None:
                user.groups = sorted(set(payload.groups))
            s.commit()
            s.refresh(user)
            return _to_user_out(user)

    # The blocking read-modify-write runs off the event loop.
    out = await asyncio.to_thread(_persist)
    if out is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    if groups_changed:
        # Group membership feeds the cached effective-permission set — drop it
        # so the change is visible on the very next request (mirrors
        # group_service.add_user_to_group/remove_user_from_group).
        from core.components.auth.logic import access_service

        access_service.invalidate_cache(username)

    response = {"status": "ok", "user": out.model_dump()}
    if hashed is not None:
        # A new password ends the sessions opened with the old one.
        response["revoked_sessions"] = await asyncio.to_thread(
            _revoke_other_sessions, str(current.username), identity
        )

    log.info(f"API: User '{username}' updated by '{identity.username}'.")
    return response


@users_router.delete("/{username}", summary="Delete a user")
def delete_user(
    username: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    # Sync (threadpool): the existence check + delete are blocking DB calls.
    from core.components.auth.logic.models import User

    target = _user_service().get_by_username(username)
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    # Canonical compare: the lookup is case-insensitive, so a differently-cased
    # path (e.g. DELETE /api/users/ADMIN by 'admin') must still be caught as
    # self and blocked.
    if _same_username(target.username, identity.username):
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    _require_superadmin_for(target, identity, "delete the account")

    from core.components.auth.logic import access_service
    from core.components.auth.logic.models import UserIdentity

    # One transaction: the account goes together with everything that could
    # still act as it or would be inherited by a later account of this name —
    # API keys and login sessions, linked external identities, preferences.
    with _db().SessionLocal() as s:
        user = s.query(User).filter(User.username == username).first()
        if user:
            _api_key_service().purge_for_username(s, str(user.username))
            s.query(UserIdentity).filter(UserIdentity.user_id == user.id).delete(synchronize_session=False)
            s.delete(user)
            s.commit()
    access_service.invalidate_cache(str(target.username))
    _forget_personal_notifications(str(target.username))

    log.info(f"API: User '{username}' deleted by '{identity.username}'.")
    return {"status": "ok", "deleted": username}


def _forget_personal_notifications(username: str) -> None:
    """Best effort: drop the notifications addressed to a deleted account and
    its read/dismissed state, so a later account of that name starts empty."""
    try:
        from core.components.notifications.model.models import NotificationRow, NotificationUserState

        with _db().SessionLocal() as s:
            s.query(NotificationUserState).filter(NotificationUserState.user_id == username).delete(
                synchronize_session=False)
            s.query(NotificationRow).filter(NotificationRow.user_id == username).delete(
                synchronize_session=False)
            s.commit()
    except Exception as exc:  # table not created yet, DB hiccup — never fail the delete over it
        log.debug(f"API: Notification cleanup for deleted user '{username}' skipped: {exc}")


# ── Per-user API key management ─────────────────────────────────────────────

@users_router.get("/{username}/api-keys", summary="List API keys for a user")
def list_api_keys(
    username: str,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Self-service: any authenticated user may list their own keys (they are
    bounded by the owner's effective permissions anyway, same as password
    self-service). Listing another user's keys requires ``admin:read``."""
    if username != identity.username and not identity.allows("admin:read"):
        raise HTTPException(status_code=403, detail="Missing required permission: admin:read")
    # Sync (threadpool): the lookups are blocking DB reads.
    if not _user_service().get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    keys = [_key_to_out(k).model_dump() for k in _api_key_service().list_for_user(username)]
    return {"status": "ok", "username": username, "api_keys": keys}


@users_router.post("/{username}/api-keys", summary="Create an API key for a user", status_code=201)
def create_api_key(
    username: str,
    payload: ApiKeyCreateRequest,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Self-service: any authenticated user may mint their own keys. Creating
    a key for another user requires ``admin:write``."""
    # A scoped key cannot mint keys at all — so every key in existence is
    # bounded by the credential that created it (new scopes ⊆ minter's scopes).
    _require_unscoped(identity, "create API keys")
    if username != identity.username and not identity.allows("admin:write"):
        raise HTTPException(status_code=403, detail="Missing required permission: admin:write")
    # Sync (threadpool): the lookup + key insert are blocking DB calls.
    target = _user_service().get_by_username(username)
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    _require_superadmin_for(target, identity, "create API keys")
    from core.components.auth.logic.api_key_service import SESSION_LABEL_PREFIX, is_session_label

    if is_session_label(payload.label):
        # The prefix marks login sessions (expiry, sign-out on password change).
        raise HTTPException(
            status_code=422,
            detail=f"The label prefix '{SESSION_LABEL_PREFIX}' is reserved for login sessions; choose another name",
        )

    raw_key, info = _api_key_service().create(username, label=payload.label, scopes=payload.scopes)
    log.info(f"API: API key '{info.label}' created for '{username}' by '{identity.username}'.")
    return {
        "status": "ok",
        "username": username,
        "token": raw_key,
        "api_key": _key_to_out(info).model_dump(),
    }


@users_router.delete("/{username}/api-keys/{key_id}", summary="Revoke an API key")
def revoke_api_key(
    username: str,
    key_id: int,
    identity: ApiIdentity = Depends(require_api_auth),
):
    """Self-service: any authenticated user may revoke their own keys. Revoking
    another user's key requires ``admin:write``."""
    if identity.is_scoped and not (username == identity.username and key_id == identity.key_id):
        # A scoped key may retire ITSELF, nothing else.
        _require_unscoped(identity, "revoke other API keys or sessions")
    if username != identity.username and not identity.allows("admin:write"):
        raise HTTPException(status_code=403, detail="Missing required permission: admin:write")
    # Sync (threadpool): the lookup + revoke are blocking DB calls.
    target = _user_service().get_by_username(username)
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    _require_superadmin_for(target, identity, "revoke API keys or sessions")
    ok = _api_key_service().delete(key_id, username=username)
    if not ok:
        raise HTTPException(status_code=404, detail=f"API key {key_id} not found for '{username}'")
    log.info(f"API: API key {key_id} deleted for '{username}' by '{identity.username}'.")
    return {"status": "ok", "deleted": key_id}
