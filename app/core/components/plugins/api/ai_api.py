"""AI assistant discovery (core API 1.12): ``GET /api/ai/plugins``.

The index of every loaded plugin that describes itself for an AI assistant —
built purely from the module registry (manifest ``ai_instructions`` = short
purpose, ``ai_instructions_route`` = where the plugin serves its full guide).
The core stores no instruction text and knows no plugin: a plugin without both
fields simply does not appear. Consumers (the AI assistant plugin, external
agents) read the index, then fetch a plugin's guide from the plugin itself when
they need it, so the text is always the running version's.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from fastapi import APIRouter, Depends

from core.api.security import ApiIdentity, require_permission

ai_router = APIRouter(prefix="/api/ai", tags=["AI"])

PLUGIN_API_PREFIX = "/api/plugins"
_LISTED_STATUSES = ("active", "remote")


def build_plugin_index(manifests: Iterable, status_of: Callable[[str], Optional[str]]) -> list[dict]:
    """Pure function: index entries for plugins that describe themselves.

    ``remote`` (pinned to another role) counts — the plugin is enabled cluster-wide
    and its guide is reachable through the load-balancer route of that instance.
    """
    out: list[dict] = []
    for m in manifests:
        if getattr(m, "type", "PLUGIN") != "PLUGIN":
            continue
        purpose = (getattr(m, "ai_instructions", None) or "").strip()
        route = getattr(m, "ai_instructions_route", None)
        if not purpose and not route:
            continue
        status = status_of(str(m.id))
        if status not in _LISTED_STATUSES:
            continue
        out.append({
            "id": str(m.id),
            "name": str(getattr(m, "name", m.id)),
            "version": str(getattr(m, "version", "") or ""),
            "status": status,
            "purpose": purpose or None,
            "instructions_url": f"{PLUGIN_API_PREFIX}/{m.id}{route}" if route else None,
        })
    out.sort(key=lambda e: e["id"])
    return out


@ai_router.get("/plugins", summary="Plugins that describe themselves for an AI assistant")
async def list_ai_plugins(_identity: ApiIdentity = Depends(require_permission("api:read"))):
    from core.api import module_registry  # resolved lazily (import order at boot)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plugins": build_plugin_index(module_registry.get_manifests(), module_registry.status),
    }
