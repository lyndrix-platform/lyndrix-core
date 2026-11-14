"""Single source of truth for reading/writing core runtime configuration.

Both the REST endpoint (``POST /api/system/config``) and the NiceGUI settings
page persist to the same Vault secret (``lyndrix/core/settings``). Previously each
re-implemented the read-merge-write + validation + runtime-apply, which meant the
NiceGUI page owned business logic and the two paths could drift. This service is
the one place that logic lives, so either UI engine is a thin client over it.

Only the settings an operator is meant to change at runtime are accepted: the
``EDITABLE_SETTINGS`` registry plus the two git tokens. Every other ``Settings``
field (CORS origins — which also are the OIDC redirect allowlist —, Vault TLS
verification, storage paths, …) is deployment configuration and stays
environment-only; the system API key needs an explicitly privileged caller.

Raises:
  * ``ValueError`` — unsupported keys or failed Settings validation (caller maps
    to HTTP 422 / a UI warning).
  * ``PermissionError`` — the system API key was touched without
    ``allow_system_api_key`` (caller maps to HTTP 403).
  * ``RuntimeError`` — Vault unavailable, current secret unreadable or write
    failed (caller maps to 503). Nothing was persisted or applied.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from core.logger import get_logger

log = get_logger("Core:SystemConfig")

_VAULT_PATH = "core/settings"
# Keys persisted in core/settings that are not pydantic Settings fields.
_TOKEN_KEYS = {"github_token", "gitlab_token"}
# The master machine-to-machine key: whoever sets it holds a system identity
# that bypasses every permission check — superadmin/system callers only.
SYSTEM_API_KEY = "system_api_key"


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
        allow_system_api_key: bool = False,
    ) -> Dict[str, Any]:
        """Validate ``updates``, persist them into ``core/settings``, and apply the
        Settings-backed ones to the live ``settings`` object.

        ``remove_keys`` are deleted from the stored secret (e.g. clearing the
        system API key). ``allow_system_api_key`` must be passed by a caller
        that has established a superadmin/system identity; without it, setting
        or removing ``system_api_key`` raises ``PermissionError``.
        Returns a small summary dict.
        """
        from config import EDITABLE_SETTINGS, Settings, settings
        from pydantic import ValidationError

        from core.components.vault.logic.kv_helper import update_secret_dict

        remove_keys = list(remove_keys or [])
        touched = set(updates) | set(remove_keys)
        if SYSTEM_API_KEY in touched and not allow_system_api_key:
            raise PermissionError("Only a superadmin may set or clear the system API key")

        editable_fields = {spec.field for spec in EDITABLE_SETTINGS}
        allowed = editable_fields | _TOKEN_KEYS | {SYSTEM_API_KEY}
        # ``remove_keys`` only ever deletes from the stored secret (internal
        # callers; cleaning up a stale key is harmless) — what may be WRITTEN
        # is the allowlist.
        unknown = sorted(set(updates) - allowed)
        if unknown:
            raise ValueError(
                f"Unsupported config keys: {', '.join(unknown)}. Only the editable settings "
                f"(GET /api/system/config → editable_settings) can be changed at runtime; "
                f"everything else is set through the environment."
            )

        settings_updates = {k: v for k, v in updates.items() if k in editable_fields}

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
            def _merge(data: Dict[str, Any]) -> None:
                data.update(updates)
                for key in remove_keys:
                    data.pop(key, None)

            # Never write after a failed read: ``core/settings`` also holds the
            # git tokens and the system API key, and a KV v2 write replaces the
            # whole secret. VaultReadError/VaultWriteError are RuntimeErrors, so
            # the callers' existing "Vault unavailable" handling (503) applies.
            update_secret_dict(_VAULT_PATH, _merge)

            if SYSTEM_API_KEY in touched:
                # The resolved key is cached for 30 s per process; a rotated or
                # cleared key must stop (or start) working at once here. Peers
                # drop theirs on the bridged ``settings:changed`` below.
                from core.api.security import invalidate_system_api_key_cache

                invalidate_system_api_key_cache()

        if apply_runtime and validated is not None:
            for key in settings_updates:
                setattr(settings, key, getattr(validated, key))
            if "LOG_LEVEL" in settings_updates:
                logging.getLogger().setLevel(
                    getattr(logging, str(settings_updates["LOG_LEVEL"]), logging.INFO)
                )

        if persist:
            # Bridged topic: peers re-hydrate from Vault so a theme/log-level/auth
            # change made on one instance does not wait for their next restart.
            try:
                from core.bus import bus

                bus.emit("settings:changed", {"keys": sorted(updates.keys()), "removed": sorted(remove_keys)})
            except Exception:  # pragma: no cover - never fail a save over a notification
                pass

        applied: List[str] = sorted(settings_updates) if apply_runtime else []
        return {
            "updated_keys": sorted(updates.keys()),
            "removed_keys": sorted(remove_keys),
            "persisted": persist,
            "applied_runtime": applied,
        }


system_config_service = SystemConfigService()
