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


def _serialize_config_field(cf) -> dict[str, Any]:
    """Serialize a ProviderConfigField for the API.

    Unlike the NiceGUI (which pre-fills the editable input with the secret), the
    API never returns sensitive values — it reports a ``configured`` flag instead,
    and PATCH treats a blank sensitive value as "keep the stored one".
    """
    sensitive = bool(getattr(cf, "sensitive", False))
    return {
        "key": cf.key,
        "label": cf.label,
        "env_var": cf.env_var,
        "sensitive": sensitive,
        "placeholder": cf.placeholder,
        "is_env_locked": bool(cf.is_env_locked),
        "current_value": "" if sensitive else (cf.current_value or ""),
        "configured": bool(cf.current_value),
    }


@notification_router_api.get(
    "/providers/{provider_id}/config",
    summary="List a provider's configurable fields (secrets masked)",
)
async def get_provider_config(
    provider_id: str,
    _identity: ApiIdentity = Depends(require_permission("api:read")),
):
    adapter = messaging_gateway.get_adapter(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' is not registered")
    return {
        "provider_id": provider_id,
        "display_name": adapter.display_name,
        "fields": [_serialize_config_field(cf) for cf in adapter.get_config_fields()],
    }


class _ProviderConfigBody(BaseModel):
    values: dict[str, str] = {}


@notification_router_api.patch(
    "/providers/{provider_id}/config",
    summary="Update a provider's config values (env-locked rejected, blank secret keeps existing)",
)
async def patch_provider_config(
    provider_id: str,
    body: _ProviderConfigBody,
    _identity: ApiIdentity = Depends(require_permission("api:write")),
):
    adapter = messaging_gateway.get_adapter(provider_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' is not registered")

    fields = {cf.key: cf for cf in adapter.get_config_fields()}

    # Reject up-front if any requested field is env-locked (don't half-apply).
    locked = [k for k, v in (body.values or {}).items()
              if k in fields and fields[k].is_env_locked]
    if locked:
        raise HTTPException(
            status_code=409,
            detail={"error": "env_locked", "locked_fields": locked},
        )

    saved: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for key, value in (body.values or {}).items():
        cf = fields.get(key)
        if cf is None:
            skipped.append(key)
            continue
        sval = "" if value is None else str(value)
        if getattr(cf, "sensitive", False) and not sval.strip():
            skipped.append(key)  # blank secret -> keep existing
            continue
        if adapter.save_config(key, sval):
            saved.append(key)
        else:
            failed.append(key)
    if failed:
        raise HTTPException(
            status_code=502,
            detail={"error": "save_failed", "fields": failed},
        )
    return {
        "status": "ok",
        "saved": saved,
        "skipped": skipped,
        "fields": [_serialize_config_field(cf) for cf in adapter.get_config_fields()],
    }


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
