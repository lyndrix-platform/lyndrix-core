"""Effective-permission resolution — the single authorization source of truth.

Identity & Permissions 2.0 replaces the legacy "roles are matched against
Group.name" convention: ``User.roles`` now carries system flags only
(``superadmin``, ``bot``) and NEVER resolves to permissions. Effective
permissions are::

    union(Group.permissions for g in user.groups)  ∪  user.extra_permissions

``has_permission`` additionally implements the per-plugin fallback rule so the
new namespaced ids stay compatible with existing grants:

    plugin:<id>:api:read   is satisfied by the global  api:read
    plugin:<id>:api:write  is satisfied by the global  api:write

Use this module everywhere an authorization decision is made (ApiIdentity,
NiceGUI layout guard, permissions API) — never re-derive group logic locally.
"""
from __future__ import annotations

import time
from typing import FrozenSet, Iterable, Optional

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import Group, User

log = get_logger("Core:AccessService")

# Tiny per-username TTL cache: authorization runs on the request hot path and
# the underlying data (group membership + group permissions) changes rarely.
# 5s keeps admin edits near-instant while collapsing per-request DB queries.
_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, FrozenSet[str]]] = {}


def invalidate_cache(username: Optional[str] = None) -> None:
    """Drop cached effective permissions (one user, or everyone)."""
    if username is None:
        _cache.clear()
    else:
        _cache.pop(username, None)


def get_effective_permissions(username: str) -> FrozenSet[str]:
    """Resolve the caller's effective permission set from the database.

    groups-union ∪ extra_permissions. Unknown users resolve to an empty set —
    never raises, an authorization path must not 500 on a missing row.
    """
    now = time.monotonic()
    hit = _cache.get(username)
    if hit is not None and hit[0] > now:
        return hit[1]

    perms: set[str] = set()
    try:
        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if user is not None:
                group_names = list(user.groups or [])
                if group_names:
                    for grp in s.query(Group).filter(Group.name.in_(group_names)).all():
                        perms.update(grp.permissions or [])
                perms.update(user.extra_permissions or [])
    except Exception as exc:  # pragma: no cover — defensive: authz must not 500
        log.warning(f"ACCESS: failed to resolve permissions for '{username}': {exc}")
        return frozenset()

    result = frozenset(perms)
    _cache[username] = (now + _CACHE_TTL_SECONDS, result)
    return result


def has_permission(effective: Iterable[str], required: str) -> bool:
    """True when ``required`` is satisfied by the ``effective`` permission set.

    Exact membership, plus the per-plugin compat fallback: a caller holding the
    global ``api:read`` / ``api:write`` satisfies any ``plugin:<id>:api:read`` /
    ``:api:write`` requirement, so pre-2.0 grants keep working after plugins
    adopt namespaced route guards.
    """
    eff = effective if isinstance(effective, (set, frozenset)) else set(effective)
    if required in eff:
        return True
    if required.startswith("plugin:"):
        if required.endswith(":api:read") and "api:read" in eff:
            return True
        if required.endswith(":api:write") and "api:write" in eff:
            return True
    return False


def username_has_permission(username: str, required: str) -> bool:
    """Convenience: resolve + check in one call (used by the NiceGUI guard)."""
    return has_permission(get_effective_permissions(username), required)
