"""
Permissions management API.

Exposes the permission/authorization model (the same one backing the
Settings → Permissions tab) over HTTP so groups, group permissions and per-user
direct grants can be inspected and configured programmatically.

Authentication & authorization
-------------------------------
All endpoints require a valid API identity (system key, per-user API key, HTTP
Basic or dashboard session). Reads require the ``admin:read`` permission; any
mutation requires ``admin:write`` (this API manages other users' access, so it
sits in the admin tier rather than the general ``api:read``/``api:write``
pair). The master system key and ``superadmin`` role bypass these checks,
consistent with the rest of the API surface.

Routes (mounted under ``/api/permissions``)::

    GET    /catalog                              list all known permission definitions
    GET    /groups                               list groups with their permissions
    POST   /groups                               create a group
    GET    /groups/{group_id}                    read a single group
    PATCH  /groups/{group_id}                    update a group (name/desc/perms/ldap)
    DELETE /groups/{group_id}                    delete a group
    GET    /groups/{group_id}/roles              list role ids assigned to a group
    POST   /groups/{group_id}/roles/{role_id}    assign a registered role to a group
    DELETE /groups/{group_id}/roles/{role_id}    unassign a role from a group
    POST   /groups/{group_id}/members/{username} add a user to a group
    DELETE /groups/{group_id}/members/{username} remove a user from a group
    GET    /users/{username}                     effective permissions for a user
    PUT    /users/{username}/extra               replace a user's direct grants
    POST   /users/{username}/extra/{perm}        add one direct grant
    DELETE /users/{username}/extra/{perm}        remove one direct grant
"""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api.security import ApiIdentity, require_permission
from core.logger import get_logger

log = get_logger("Core:PermissionsAPI")

permissions_router = APIRouter(prefix="/api/permissions", tags=["Permissions"])


# ── Pydantic request/response models ─────────────────────────────────────────
class PermissionDefOut(BaseModel):
    id: str
    label: str
    category: str
    description: str = ""
    icon: str = "lock"
    plugin_id: Optional[str] = None  # owning plugin for per-plugin catalog grouping


class GroupOut(BaseModel):
    id: int
    name: str
    description: str = ""
    permissions: List[str] = Field(default_factory=list)
    ldap_mappings: List[str] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list)


class GroupCreateRequest(BaseModel):
    name: str
    description: str = ""
    permissions: List[str] = Field(default_factory=list)
    ldap_mappings: List[str] = Field(default_factory=list)
    allow_unknown: bool = False


class GroupUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[List[str]] = None
    ldap_mappings: Optional[List[str]] = None
    allow_unknown: bool = False


class ExtraPermissionsRequest(BaseModel):
    permissions: List[str] = Field(default_factory=list)
    allow_unknown: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────
def _registry():
    from core.components.auth.logic.permission_registry import permission_registry

    return permission_registry


def _known_permission_ids() -> set:
    reg = _registry()
    try:
        reg.scan_from_manifests()
    except Exception:  # pragma: no cover - manifests optional at this point
        pass
    return {p.id for p in reg.get_all()}


def _validate_permissions(permissions: List[str], allow_unknown: bool) -> None:
    """Reject permission IDs that are not in the registry unless explicitly allowed."""
    if allow_unknown:
        return
    known = _known_permission_ids()
    unknown = sorted({p for p in permissions if p not in known})
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown permission ids: {', '.join(unknown)}. "
                f"Pass allow_unknown=true to override."
            ),
        )


def _group_to_out(grp) -> GroupOut:
    return GroupOut(
        id=int(grp.id),
        name=str(grp.name),
        description=str(grp.description or ""),
        permissions=list(grp.permissions or []),
        ldap_mappings=list(grp.ldap_mappings or []),
        roles=list(grp.roles or []),
    )


def _group_service():
    from core.components.auth.logic.group_service import group_service

    return group_service


def _user_service():
    from core.components.auth.logic.user_service import user_service

    return user_service


def _role_registry():
    from core.components.auth.logic.role_registry import role_registry

    return role_registry


def _invalidate_access_cache() -> None:
    from core.components.auth.logic import access_service

    access_service.invalidate_cache()


def _same_username(a: Optional[str], b: Optional[str]) -> bool:
    """Case-insensitive username equality (usernames are looked up
    case-insensitively, so a raw ``==`` would miss a differently-cased self)."""
    return (a or "").casefold() == (b or "").casefold()


def _is_superadmin(identity: ApiIdentity) -> bool:
    return identity.is_system or "superadmin" in identity.roles


# ── Catalog ──────────────────────────────────────────────────────────────────
@permissions_router.get(
    "/catalog",
    summary="List all known permission definitions",
    response_model=Dict[str, object],
)
async def list_permission_catalog(
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    """Return every permission id the system knows about (route/plugin/feature/api)."""
    reg = _registry()
    try:
        reg.scan_from_manifests()
    except Exception:
        pass
    perms = [
        PermissionDefOut(
            id=p.id,
            label=p.label,
            category=p.category,
            description=p.description,
            icon=p.icon,
            plugin_id=getattr(p, "plugin_id", None),
        ).model_dump()
        for p in reg.get_all()
    ]
    return {
        "status": "ok",
        "count": len(perms),
        "categories": reg.categories(),
        "permissions": perms,
    }


# ── Roles (manifest-declared bundles — read-only reference) ─────────────────
@permissions_router.get("/roles", summary="List manifest-declared roles")
async def list_roles(identity: ApiIdentity = Depends(require_permission("admin:read"))):
    """Read-only: roles are registry metadata, not assignable entities.

    Their effect (permissions attached to auto_map groups) is applied once via
    the RoleGrantLedger; this endpoint feeds the informational Roles panel.
    """
    from core.components.auth.logic.role_registry import role_registry

    try:
        role_registry.scan_from_manifests()
    except Exception:
        pass
    roles = [
        {
            "id": r.id,
            "label": r.label,
            "plugin_id": r.plugin_id,
            "permissions": r.permissions,
            "auto_map_groups": r.auto_map_groups,
            "description": r.description,
        }
        for r in role_registry.get_all()
    ]
    return {"status": "ok", "count": len(roles), "roles": roles}


# ── Groups ───────────────────────────────────────────────────────────────────
@permissions_router.get("/groups", summary="List groups and their permissions")
async def list_groups(identity: ApiIdentity = Depends(require_permission("admin:read"))):
    groups = [_group_to_out(g).model_dump() for g in _group_service().get_all()]
    return {"status": "ok", "count": len(groups), "groups": groups}


@permissions_router.post("/groups", summary="Create a group")
async def create_group(
    payload: GroupCreateRequest,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Group name must not be empty")

    svc = _group_service()
    if svc.get_by_name(name):
        raise HTTPException(status_code=409, detail=f"Group '{name}' already exists")

    _validate_permissions(payload.permissions, payload.allow_unknown)
    grp = svc.create(
        name=name,
        description=payload.description,
        permissions=sorted(set(payload.permissions)),
        ldap_mappings=payload.ldap_mappings,
    )
    log.info(f"API: Group '{name}' created by '{identity.username}'.")
    return {"status": "ok", "group": _group_to_out(grp).model_dump()}


@permissions_router.get("/groups/{group_id}", summary="Read a single group")
async def get_group(
    group_id: int,
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    grp = _group_service().get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    return {"status": "ok", "group": _group_to_out(grp).model_dump()}


@permissions_router.patch("/groups/{group_id}", summary="Update a group")
async def update_group(
    group_id: int,
    payload: GroupUpdateRequest,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _group_service()
    grp = svc.get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")

    permissions = None
    if payload.permissions is not None:
        _validate_permissions(payload.permissions, payload.allow_unknown)
        permissions = sorted(set(payload.permissions))

    name = payload.name.strip() if payload.name is not None else None
    if name is not None and not name:
        raise HTTPException(status_code=422, detail="Group name must not be empty")
    if name and name != grp.name and svc.get_by_name(name):
        raise HTTPException(status_code=409, detail=f"Group '{name}' already exists")

    updated = svc.update(
        group_id,
        name=name,
        description=payload.description,
        permissions=permissions,
        ldap_mappings=payload.ldap_mappings,
    )
    log.info(f"API: Group {group_id} updated by '{identity.username}'.")
    return {"status": "ok", "group": _group_to_out(updated).model_dump()}


@permissions_router.delete("/groups/{group_id}", summary="Delete a group")
async def delete_group(
    group_id: int,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _group_service()
    grp = svc.get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    name = grp.name
    ok = svc.delete(group_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete group")
    log.info(f"API: Group '{name}' ({group_id}) deleted by '{identity.username}'.")
    return {"status": "ok", "deleted": {"id": group_id, "name": name}}


# ── Group role assignment (Identity 2.0 — first-class assignable roles) ─────
# Roles are stored on Group.roles (registered role ids) and expanded to
# permissions live at resolution time (access_service.get_effective_permissions),
# rather than melted once — assigning/removing a role here takes effect for
# every member of the group after the 5s permission cache expires.

@permissions_router.get(
    "/groups/{group_id}/roles", summary="List role ids assigned to a group"
)
async def get_group_roles(
    group_id: int,
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    grp = _group_service().get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    return {"status": "ok", "group_id": group_id, "roles": list(grp.roles or [])}


@permissions_router.post(
    "/groups/{group_id}/roles/{role_id}", summary="Assign a registered role to a group"
)
async def assign_group_role(
    group_id: int,
    role_id: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _group_service()
    grp = svc.get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")

    reg = _role_registry()
    try:
        reg.scan_from_manifests()
    except Exception:  # pragma: no cover - manifests optional at this point
        pass
    if reg.get(role_id) is None:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

    updated = reg.apply_role_to_group(group_id, role_id)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    _invalidate_access_cache()
    log.info(f"API: Role '{role_id}' assigned to group {group_id} by '{identity.username}'.")
    return {"status": "ok", "group": _group_to_out(updated).model_dump()}


@permissions_router.delete(
    "/groups/{group_id}/roles/{role_id}", summary="Unassign a role from a group"
)
async def unassign_group_role(
    group_id: int,
    role_id: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _group_service()
    grp = svc.get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")

    reg = _role_registry()
    try:
        reg.scan_from_manifests()
    except Exception:  # pragma: no cover - manifests optional at this point
        pass
    if reg.get(role_id) is None:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

    updated = reg.remove_role_from_group(group_id, role_id)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    _invalidate_access_cache()
    log.info(f"API: Role '{role_id}' unassigned from group {group_id} by '{identity.username}'.")
    return {"status": "ok", "group": _group_to_out(updated).model_dump()}


# ── Group membership ─────────────────────────────────────────────────────────

@permissions_router.post(
    "/groups/{group_id}/members/{username}", summary="Add a user to a group"
)
async def add_group_member(
    group_id: int,
    username: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _group_service()
    grp = svc.get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    target = _user_service().get_by_username(username)
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    # Self group-membership guard (canonical, case-insensitive): a non-superadmin
    # admin cannot add THEMSELVES to a group — that would be a trivial bypass of
    # the same invariant users_api enforces (self group changes must go through
    # another admin). Superadmins are exempt (break-glass).
    if _same_username(target.username, identity.username) and not _is_superadmin(identity):
        raise HTTPException(
            status_code=403,
            detail="Cannot modify your own group membership; another admin must do this",
        )

    # add_user_to_group() already invalidates the caller's cached permissions
    # (group_service mirrors the same pattern as the other membership calls).
    svc.add_user_to_group(group_id, username)
    log.info(f"API: '{username}' added to group {group_id} by '{identity.username}'.")
    return {"status": "ok", "group": _group_to_out(svc.get_by_id(group_id)).model_dump()}


@permissions_router.delete(
    "/groups/{group_id}/members/{username}", summary="Remove a user from a group"
)
async def remove_group_member(
    group_id: int,
    username: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _group_service()
    grp = svc.get_by_id(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    target = _user_service().get_by_username(username)
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    # Same self group-membership guard as add_group_member: a non-superadmin
    # admin cannot pull THEMSELVES out of a group either (self group changes go
    # through another admin). Superadmins are exempt.
    if _same_username(target.username, identity.username) and not _is_superadmin(identity):
        raise HTTPException(
            status_code=403,
            detail="Cannot modify your own group membership; another admin must do this",
        )

    svc.remove_user_from_group(group_id, username)
    log.info(f"API: '{username}' removed from group {group_id} by '{identity.username}'.")
    return {"status": "ok", "group": _group_to_out(svc.get_by_id(group_id)).model_dump()}


# ── User direct grants ───────────────────────────────────────────────────────
@permissions_router.get(
    "/users/{username}", summary="Effective permissions for a user"
)
async def get_user_permissions(
    username: str,
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    user = _user_service().get_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    roles = list(user.roles or [])
    groups = list(user.groups or [])
    extra = list(user.extra_permissions or [])

    # Reuse the SAME resolver real authorization uses so this admin-facing
    # "what can this user do" view matches reality — group.permissions PLUS
    # assigned-role expansion (Group.roles) PLUS direct grants. Deriving it
    # from group_service.get_permissions_for_roles() alone would silently
    # under-report every permission held through an assigned group role.
    from core.components.auth.logic import access_service

    effective_set = access_service.get_effective_permissions(str(user.username))
    effective = sorted(effective_set)
    # Group-derived subset (perms + role expansion), i.e. everything not a
    # direct grant — keeps the response field accurate for the same reason.
    from_groups = sorted(effective_set - set(extra))

    return {
        "status": "ok",
        "user": {
            "username": str(user.username),
            "roles": roles,
            "groups": groups,
            "extra_permissions": extra,
            "from_groups": from_groups,
            "effective_permissions": effective,
        },
    }


@permissions_router.put(
    "/users/{username}/extra", summary="Replace a user's direct permission grants"
)
async def set_user_extra_permissions(
    username: str,
    payload: ExtraPermissionsRequest,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _user_service()
    if not svc.get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    _validate_permissions(payload.permissions, payload.allow_unknown)
    ok = svc.update_extra_permissions(username, payload.permissions)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update permissions")
    log.info(f"API: Extra permissions for '{username}' set by '{identity.username}'.")
    user = svc.get_by_username(username)
    return {
        "status": "ok",
        "username": username,
        "extra_permissions": list(user.extra_permissions or []),
    }


@permissions_router.post(
    "/users/{username}/extra/{permission:path}",
    summary="Grant a single direct permission to a user",
)
async def add_user_extra_permission(
    username: str,
    permission: str,
    allow_unknown: bool = False,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _user_service()
    if not svc.get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    _validate_permissions([permission], allow_unknown)
    svc.add_extra_permission(username, permission)
    log.info(
        f"API: Granted '{permission}' to '{username}' by '{identity.username}'."
    )
    user = svc.get_by_username(username)
    return {
        "status": "ok",
        "username": username,
        "extra_permissions": list(user.extra_permissions or []),
    }


@permissions_router.delete(
    "/users/{username}/extra/{permission:path}",
    summary="Revoke a single direct permission from a user",
)
async def remove_user_extra_permission(
    username: str,
    permission: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _user_service()
    if not svc.get_by_username(username):
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    svc.remove_extra_permission(username, permission)
    log.info(
        f"API: Revoked '{permission}' from '{username}' by '{identity.username}'."
    )
    user = svc.get_by_username(username)
    return {
        "status": "ok",
        "username": username,
        "extra_permissions": list(user.extra_permissions or []),
    }


__all__ = ["permissions_router"]
