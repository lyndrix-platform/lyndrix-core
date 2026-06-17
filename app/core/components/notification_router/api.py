"""FastAPI surface for the notification router."""
from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.api.security import ApiIdentity, require_permission
from core.components.messaging.adapter import GatewayCapability
from core.components.messaging.gateway import messaging_gateway

from .logic.endpoint_registry import endpoint_registry
from .logic.precedence import (
    GLOBAL_DEFAULT_PROVIDER_ENV,
    env_active_var,
    env_is_locked_active,
    env_is_locked_provider,
    env_provider_var,
    resolve_endpoint_state,
)
from .logic.router_service import notification_router


notification_router_api = APIRouter(prefix="/api/notifications", tags=["Notifications"])


def _plugin_name(plugin_id: str) -> str:
    try:
        from core.components.plugins.logic.manager import module_manager
        entry = module_manager.registry.get(plugin_id)
        if entry and entry.get("manifest"):
            return entry["manifest"].name
    except Exception:
        pass
    return plugin_id


def _serialize_endpoint(plugin_id: str, endpoint) -> dict[str, Any]:
    state = resolve_endpoint_state(plugin_id, endpoint.name)
    return {
        "plugin_id": plugin_id,
        "plugin_name": _plugin_name(plugin_id),
        "endpoint_name": endpoint.name,
        "description": endpoint.description,
        "active": state.active,
        "provider": state.provider,
        "active_source": state.active_source,
        "provider_source": state.provider_source,
        "active_is_env_locked": env_is_locked_active(plugin_id, endpoint.name),
        "provider_is_env_locked": env_is_locked_provider(plugin_id, endpoint.name),
        "declared_defaults": endpoint.model_dump(),
        "env_var_active": env_active_var(plugin_id, endpoint.name),
        "env_var_provider": env_provider_var(plugin_id, endpoint.name),
    }


@notification_router_api.get(
    "/endpoints",
    summary="List all discovered notification endpoints",
)
async def list_endpoints(
    _identity: ApiIdentity = Depends(require_permission("api:read")),
):
    items = [_serialize_endpoint(pid, ep) for pid, ep in endpoint_registry.iter_all()]
    items.sort(key=lambda item: (item["plugin_name"].lower(), item["endpoint_name"]))
    return {"endpoints": items}


@notification_router_api.get(
    "/providers",
    summary="List registered messaging gateway providers",
)
async def list_providers(
    _identity: ApiIdentity = Depends(require_permission("api:read")),
):
    providers = []
    for adapter in messaging_gateway.list_adapters():
        caps = [c.name for c in GatewayCapability if adapter.supports(c)]
        providers.append({
            "provider_id": adapter.provider_id,
            "display_name": adapter.display_name,
            "capabilities": caps,
        })
    return {
        "providers": providers,
        "global_default": os.getenv(GLOBAL_DEFAULT_PROVIDER_ENV),
        "global_default_env_var": GLOBAL_DEFAULT_PROVIDER_ENV,
    }


@notification_router_api.get(
    "/providers/{provider_id}/health",
    summary="Check health of a specific registered provider",
)
async def provider_health(
    provider_id: str,
    _identity: ApiIdentity = Depends(require_permission("api:read")),
):
    status = await messaging_gateway.provider_health(provider_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' is not registered")
    return {"provider_id": provider_id, "healthy": status}


class _PatchBody(BaseModel):
    active: bool | None = None
    provider: str | None | Literal["__unset__"] = "__unset__"


@notification_router_api.patch(
    "/endpoints/{plugin_id}/{endpoint_name}",
    summary="Update active state and/or provider binding for an endpoint",
)
async def patch_endpoint(
    plugin_id: str,
    endpoint_name: str,
    body: _PatchBody,
    _identity: ApiIdentity = Depends(require_permission("api:write")),
):
    if endpoint_registry.get(plugin_id, endpoint_name) is None:
        raise HTTPException(status_code=404, detail="Unknown endpoint")

    locked: dict[str, str] = {}
    if body.active is not None and env_is_locked_active(plugin_id, endpoint_name):
        locked["active"] = env_active_var(plugin_id, endpoint_name)
    if body.provider != "__unset__" and env_is_locked_provider(plugin_id, endpoint_name):
        locked["provider"] = env_provider_var(plugin_id, endpoint_name)
    if locked:
        raise HTTPException(
            status_code=409,
            detail={"error": "env_locked", "locked_fields": locked},
        )

    provider_arg: Any
    if body.provider == "__unset__":
        from .logic.router_service import _UNSET as _UNSET_SENTINEL  # noqa: WPS437
        provider_arg = _UNSET_SENTINEL
    else:
        provider_arg = body.provider

    state = await notification_router.update_binding(
        plugin_id,
        endpoint_name,
        active=body.active,
        provider=provider_arg,
    )
    return {"status": "ok", "state": state.model_dump()}
