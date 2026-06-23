"""Active-theme selection — one path shared by the REST API and NiceGUI.

The active theme is just the ``DEFAULT_THEME_ID`` key in ``core/settings``, so this
delegates persistence to ``system_config_service`` and adds the theme-specific
concerns: validating the theme exists and emitting ``ui:needs_refresh``.
"""
from __future__ import annotations

from core.logger import get_logger

log = get_logger("Core:ThemesService")


def set_active(theme_id: str) -> None:
    """Persist + apply the active theme, then signal the UI to refresh.

    Raises ``ValueError`` if the theme is unknown, ``RuntimeError`` if Vault is
    unavailable (propagated from ``system_config_service``).
    """
    from core.theming import get_theme_engine

    if theme_id not in get_theme_engine().list_available_themes():
        raise ValueError(f"Theme '{theme_id}' not found")

    from .system_config_service import system_config_service

    system_config_service.apply({"DEFAULT_THEME_ID": theme_id}, persist=True, apply_runtime=True)

    from core.bus import bus

    bus.emit("ui:needs_refresh", {"reason": "theme_changed"})
    log.info("Active theme set to '%s'.", theme_id)
