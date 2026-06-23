"""Single source of truth for reading/writing core runtime configuration.

Both the REST endpoint (``POST /api/system/config``) and the NiceGUI settings
page persist to the same Vault secret (``lyndrix/core/settings``). Previously each
re-implemented the read-merge-write + validation + runtime-apply, which meant the
NiceGUI page owned business logic and the two paths could drift. This service is
the one place that logic lives, so either UI engine is a thin client over it.

Raises:
  * ``ValueError`` — unsupported keys or failed Settings validation (caller maps
    to HTTP 422 / a UI warning).
  * ``RuntimeError`` — Vault unavailable / write failed (caller maps to 503).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from core.logger import get_logger

log = get_logger("Core:SystemConfig")

_VAULT_PATH = "core/settings"
# Keys persisted in core/settings that are not pydantic Settings fields.
_ALLOWED_EXTRA_KEYS = {"github_token", "gitlab_token", "system_api_key"}


class SystemConfigService:
    """Validate, persist, and runtime-apply core configuration updates."""

    def read_all(self) -> Dict[str, Any]:
        """Return the full ``core/settings`` secret ({} if unset/unreachable)."""
        from core.components.vault.logic.kv_helper import read_secret_dict

        return read_secret_dict(_VAULT_PATH)

    def get_value(self, key: str, default: Any = None) -> Any:
        return self.read_all().get(key, default)

    def apply(
        self,
        updates: Dict[str, Any],
        *,
        persist: bool = True,
        apply_runtime: bool = True,
        remove_keys: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Validate ``updates``, persist them into ``core/settings``, and apply the
        Settings-backed ones to the live ``settings`` object.

        ``remove_keys`` are deleted from the stored secret (e.g. clearing the
        system API key). Returns a small summary dict.
        """
        from config import Settings, settings
        from pydantic import ValidationError

        from core.components.vault.logic.kv_helper import read_secret_dict, write_secret_dict

        remove_keys = list(remove_keys or [])
        settings_fields = set(settings.model_fields.keys())
        unknown = sorted(set(updates) - settings_fields - _ALLOWED_EXTRA_KEYS)
        if unknown:
            raise ValueError(f"Unsupported config keys: {', '.join(unknown)}")

        settings_updates = {k: v for k, v in updates.items() if k in settings_fields}

        validated: Optional[Settings] = None
        if settings_updates:
            try:
                merged = settings.model_dump()
                merged.update(settings_updates)
                validated = Settings.model_validate(merged)
            except ValidationError as exc:
                # Surface the structured errors to the caller (API → 422 body).
                raise ValueError(exc.errors()) from exc

        if persist:
            data = read_secret_dict(_VAULT_PATH)
            data.update(updates)
            for key in remove_keys:
                data.pop(key, None)
            if not write_secret_dict(_VAULT_PATH, data):
                raise RuntimeError("Vault unavailable — configuration was not persisted")

        if apply_runtime and validated is not None:
            for key in settings_updates:
                setattr(settings, key, getattr(validated, key))
            if "LOG_LEVEL" in settings_updates:
                logging.getLogger().setLevel(
                    getattr(logging, str(settings_updates["LOG_LEVEL"]), logging.INFO)
                )

        applied: List[str] = sorted(settings_updates) if apply_runtime else []
        return {
            "updated_keys": sorted(updates.keys()),
            "removed_keys": sorted(remove_keys),
            "persisted": persist,
            "applied_runtime": applied,
        }


system_config_service = SystemConfigService()
