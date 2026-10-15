"""GET /api/nav — sidebar entries plugins registered at runtime (core API ≥ 1.9).

The shell renders these next to the plugin apps (grouped by ``group``); an entry
with ``open_mode == "iframe"`` opens embedded at ``/ext/<plugin>/<id>``, one
with ``"new_tab"`` as a plain external link. Entries of plugins the caller may
not see (no ``plugin:<id>`` grant, see ``/api/me/access``) are filtered out
server-side — the shell never has to re-implement that.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.api.security import ApiIdentity, require_api_auth
from core.components.plugins.logic.nav import nav_registry
from core.logger import get_logger

log = get_logger("Core:NavAPI")

nav_router = APIRouter(prefix="/api/nav", tags=["Navigation"])


def _visible_plugins(identity: ApiIdentity, candidates: list[str]) -> list[str]:
    """Plugins among ``candidates`` whose umbrella grant ``plugin:<id>`` the caller has."""
    if identity.is_system or "superadmin" in identity.roles:
        return candidates
    try:
        from core.components.auth.logic import access_service

        effective = access_service.get_effective_permissions(identity.username)
        return [pid for pid in candidates if access_service.has_permission(effective, f"plugin:{pid}")]
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"NAV API: visibility derivation failed: {exc}")
        return []


@nav_router.get("", summary="Sidebar entries registered by plugins")
def list_nav_entries(identity: ApiIdentity = Depends(require_api_auth)):
    """Sync ``def`` (threadpool): the permission lookup reads the DB."""
    visible = _visible_plugins(identity, nav_registry.plugin_ids())
    entries = nav_registry.entries(visible)
    return {"status": "ok", "count": len(entries), "entries": entries}
