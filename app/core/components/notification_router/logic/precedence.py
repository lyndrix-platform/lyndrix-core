"""Hierarchical precedence resolver for notification endpoint state.

Order: env var > DB row > manifest declaration default.

Env var naming convention:
    LYNDRIX_NOTIF__<PLUGIN_ID_NORMALIZED>__<ENDPOINT_NAME_UPPER>__ACTIVE
    LYNDRIX_NOTIF__<PLUGIN_ID_NORMALIZED>__<ENDPOINT_NAME_UPPER>__PROVIDER
    LYNDRIX_NOTIF__<PLUGIN_ID_NORMALIZED>__<ENDPOINT_NAME_UPPER>__PERMISSION
    LYNDRIX_NOTIF_DEFAULT_PROVIDER

``PLUGIN_ID_NORMALIZED`` = uppercase, with ``.`` and ``-`` replaced by ``_``.

Routing v2: the PROVIDER value is a COMMA-SEPARATED list of provider ids
(fan-out); a present-but-empty env var still means "env-locked to no external
providers". PERMISSION is single-valued and TRI-STATE at the DB layer:
NULL = manifest default, "" = explicitly unrestricted, value = required id.
"""
from __future__ import annotations

import os
from typing import Any

from ..models import ResolvedState
from .endpoint_registry import endpoint_registry


GLOBAL_DEFAULT_PROVIDER_ENV = "LYNDRIX_NOTIF_DEFAULT_PROVIDER"
ENV_PREFIX = "LYNDRIX_NOTIF"


def normalize_segment(value: str) -> str:
    """Convert a plugin id like 'lyndrix.plugin.foo-bar' to 'LYNDRIX_PLUGIN_FOO_BAR'."""
    return value.upper().replace(".", "_").replace("-", "_")


def env_active_var(plugin_id: str, endpoint_name: str) -> str:
    return f"{ENV_PREFIX}__{normalize_segment(plugin_id)}__{endpoint_name.upper()}__ACTIVE"


def env_provider_var(plugin_id: str, endpoint_name: str) -> str:
    return f"{ENV_PREFIX}__{normalize_segment(plugin_id)}__{endpoint_name.upper()}__PROVIDER"


def env_permission_var(plugin_id: str, endpoint_name: str) -> str:
    return f"{ENV_PREFIX}__{normalize_segment(plugin_id)}__{endpoint_name.upper()}__PERMISSION"


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_provider_list(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def env_is_locked_active(plugin_id: str, endpoint_name: str) -> bool:
    return os.getenv(env_active_var(plugin_id, endpoint_name)) is not None


def env_is_locked_provider(plugin_id: str, endpoint_name: str) -> bool:
    return os.getenv(env_provider_var(plugin_id, endpoint_name)) is not None


def env_is_locked_permission(plugin_id: str, endpoint_name: str) -> bool:
    return os.getenv(env_permission_var(plugin_id, endpoint_name)) is not None


# Db cache: (plugin_id, endpoint_name) -> {"is_active", "provider_bindings",
# "required_permission_override"} mirroring the PluginNotificationEndpoint row.
_db_cache: dict[tuple[str, str], dict[str, Any]] = {}


def update_db_cache(
    plugin_id: str,
    endpoint_name: str,
    *,
    is_active: bool | None,
    provider_bindings: list[str] | None,
    required_permission_override: str | None = None,
) -> None:
    _db_cache[(plugin_id, endpoint_name)] = {
        "is_active": is_active,
        "provider_bindings": list(provider_bindings) if provider_bindings is not None else None,
        "required_permission_override": required_permission_override,
    }


def remove_from_db_cache(plugin_id: str, endpoint_name: str) -> None:
    _db_cache.pop((plugin_id, endpoint_name), None)


def remove_plugin_from_db_cache(plugin_id: str) -> None:
    for key in [k for k in _db_cache if k[0] == plugin_id]:
        del _db_cache[key]


def db_cache_get(plugin_id: str, endpoint_name: str) -> dict[str, Any] | None:
    return _db_cache.get((plugin_id, endpoint_name))


def resolve_endpoint_state(plugin_id: str, endpoint_name: str) -> ResolvedState:
    decl = endpoint_registry.get(plugin_id, endpoint_name)
    if decl is None:
        return ResolvedState(active=False, providers=[],
                             active_source="default", provider_source="none")

    row = db_cache_get(plugin_id, endpoint_name)

    # --- ACTIVE ---
    raw_env_active = os.getenv(env_active_var(plugin_id, endpoint_name))
    if raw_env_active is not None:
        active = _parse_bool(raw_env_active)
        active_source: str = "env"
    elif row is not None and row.get("is_active") is not None:
        active = bool(row["is_active"])
        active_source = "db"
    else:
        active = decl.default_active
        active_source = "default"

    # --- PROVIDERS (CSV env / JSON-list DB / global default) ---
    raw_env_provider = os.getenv(env_provider_var(plugin_id, endpoint_name))
    if raw_env_provider is not None:
        providers = _parse_provider_list(raw_env_provider)
        provider_source: str = "env"
    elif row is not None and row.get("provider_bindings") is not None:
        providers = [p for p in (row["provider_bindings"] or []) if p]
        provider_source = "db"
    elif decl.external_default:
        global_default = os.getenv(GLOBAL_DEFAULT_PROVIDER_ENV)
        cleaned = (global_default.strip() if global_default else "") or ""
        providers = [cleaned] if cleaned else []
        provider_source = "global_default" if providers else "none"
    else:
        providers = []
        provider_source = "none"

    # --- REQUIRED PERMISSION (tri-state DB: NULL default / "" open / value) ---
    raw_env_perm = os.getenv(env_permission_var(plugin_id, endpoint_name))
    if raw_env_perm is not None:
        required_permission = raw_env_perm.strip() or None
        permission_source: str = "env"
    elif row is not None and row.get("required_permission_override") is not None:
        override = str(row["required_permission_override"])
        required_permission = override or None  # "" -> explicitly unrestricted
        permission_source = "db"
    else:
        required_permission = getattr(decl, "required_permission", None) or None
        permission_source = "default" if required_permission else "none"

    return ResolvedState(
        active=active,
        providers=providers,
        active_source=active_source,  # type: ignore[arg-type]
        provider_source=provider_source,  # type: ignore[arg-type]
        required_permission=required_permission,
        required_permission_source=permission_source,  # type: ignore[arg-type]
    )
