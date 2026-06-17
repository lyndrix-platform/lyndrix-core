"""Hierarchical precedence resolver for notification endpoint state.

Order: env var > DB row > manifest declaration default.

Env var naming convention:
    LYNDRIX_NOTIF__<PLUGIN_ID_NORMALIZED>__<ENDPOINT_NAME_UPPER>__ACTIVE
    LYNDRIX_NOTIF__<PLUGIN_ID_NORMALIZED>__<ENDPOINT_NAME_UPPER>__PROVIDER
    LYNDRIX_NOTIF_DEFAULT_PROVIDER

``PLUGIN_ID_NORMALIZED`` = uppercase, with ``.`` and ``-`` replaced by ``_``.
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


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_is_locked_active(plugin_id: str, endpoint_name: str) -> bool:
    return os.getenv(env_active_var(plugin_id, endpoint_name)) is not None


def env_is_locked_provider(plugin_id: str, endpoint_name: str) -> bool:
    return os.getenv(env_provider_var(plugin_id, endpoint_name)) is not None


# Db cache: plugin_id, endpoint_name -> {"is_active": Optional[bool], "provider_binding": Optional[str]}
_db_cache: dict[tuple[str, str], dict[str, Any]] = {}


def update_db_cache(plugin_id: str, endpoint_name: str, *, is_active: bool | None, provider_binding: str | None) -> None:
    _db_cache[(plugin_id, endpoint_name)] = {
        "is_active": is_active,
        "provider_binding": provider_binding,
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
        return ResolvedState(active=False, provider=None,
                             active_source="default", provider_source="none")

    # --- ACTIVE ---
    raw_env_active = os.getenv(env_active_var(plugin_id, endpoint_name))
    if raw_env_active is not None:
        active = _parse_bool(raw_env_active)
        active_source: str = "env"
    else:
        row = db_cache_get(plugin_id, endpoint_name)
        if row is not None and row.get("is_active") is not None:
            active = bool(row["is_active"])
            active_source = "db"
        else:
            active = decl.default_active
            active_source = "default"

    # --- PROVIDER ---
    raw_env_provider = os.getenv(env_provider_var(plugin_id, endpoint_name))
    if raw_env_provider is not None:
        cleaned = raw_env_provider.strip()
        provider = cleaned or None
        provider_source: str = "env"
    else:
        row = db_cache_get(plugin_id, endpoint_name)
        if row is not None and row.get("provider_binding") is not None:
            provider = row["provider_binding"] or None
            provider_source = "db"
        elif decl.external_default:
            global_default = os.getenv(GLOBAL_DEFAULT_PROVIDER_ENV)
            provider = (global_default.strip() if global_default else None) or None
            provider_source = "global_default" if provider else "none"
        else:
            provider = None
            provider_source = "none"

    return ResolvedState(
        active=active,
        provider=provider,
        active_source=active_source,  # type: ignore[arg-type]
        provider_source=provider_source,  # type: ignore[arg-type]
    )
