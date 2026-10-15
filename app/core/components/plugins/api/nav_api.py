"""GET /api/nav — sidebar entries plugins registered at runtime (core API ≥ 1.9).

The shell renders these next to the plugin apps (grouped by ``group``); an entry
with ``open_mode == "iframe"`` opens embedded at ``/ext/<plugin>/<id>``, one
with ``"new_tab"`` as a plain external link.

Authorization mirrors the plugin list: the caller needs ``api:read`` (as for
``GET /api/plugins``) and sees only entries of plugins it may see — the
umbrella grant ``plugin:<id>`` exactly as ``/api/me/access`` derives
``visible`` (superadmin / the system key see everything). Filtering happens
server-side so no shell has to re-implement it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.api.security import ApiIdentity, require_permission
from core.components.plugins.logic.nav import nav_registry
from core.logger import get_logger

log = get_logger("Core:NavAPI")

nav_router = APIRouter(prefix="/api/nav", tags=["Navigation"])


def _visible_plugins(identity: ApiIdentity, candidates: list[str]) -> list[str]:
    """Plugins among ``candidates`` whose umbrella grant ``plugin:<id>`` the caller holds.

    Fails closed: when the permission set cannot be resolved, nothing is visible.
    """
    if identity.is_system or "superadmin" in (identity.roles or []):
        return list(candidates)
    try:
        from core.components.auth.logic import access_service

        effective = identity.effective_permissions()
        return [pid for pid in candidates if access_service.has_permission(effective, f"plugin:{pid}")]
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"NAV API: visibility derivation failed for '{identity.username}': {exc}")
        return []


@nav_router.get("", summary="Sidebar entries registered by plugins")
def list_nav_entries(identity: ApiIdentity = Depends(require_permission("api:read"))):
    """Sync ``def`` (threadpool): resolving the permission set may read the DB."""
    visible = _visible_plugins(identity, nav_registry.plugin_ids())
    entries = nav_registry.entries(visible)
    return {"status": "ok", "count": len(entries), "entries": entries}
