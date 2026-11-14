"""Active-theme selection — one path shared by the REST API and NiceGUI.

The active theme is just the ``DEFAULT_THEME_ID`` key in ``core/settings``, so this
delegates persistence to ``system_config_service`` and adds the theme-specific
concerns: validating the theme exists and emitting ``ui:needs_refresh``.
"""
from __future__ import annotations

from core.logger import get_logger

log = get_logger("Core:ThemesService")


class ThemeRejected(ValueError):
    """An uploaded theme pack is malformed or carries unsafe values."""


def check_uploaded_pack(theme_id: str, tokens_raw: bytes, components_raw: bytes) -> None:
    """Validate an uploaded pack before a single byte is written (SC-02).

    Theme values are rendered into ``<style>`` and HTML attributes of every
    page, so an unsafe pack is refused outright instead of being stored with
    warnings. Shared by the REST upload and the NiceGUI settings page. Raises
    ``ThemeRejected`` with a message that names the offending keys.
    """
    import json

    from pydantic import ValidationError

    from core.theming.models import ThemeComponents, ThemePack, ThemeTokens
    from core.theming.schema import theme_pack_errors

    try:
        pack = ThemePack(
            theme_id=theme_id,
            tokens=ThemeTokens(**json.loads(tokens_raw)),
            components=ThemeComponents(**json.loads(components_raw)),
        )
    except (ValueError, TypeError, ValidationError) as exc:
        raise ThemeRejected(
            "Theme validation failed: tokens.json/components.json are not valid "
            f"theme files ({type(exc).__name__})"
        ) from exc

    errors = theme_pack_errors(pack)
    if errors:
        shown = "; ".join(errors[:5])
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        raise ThemeRejected(f"Theme rejected: {shown}{more}")


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
