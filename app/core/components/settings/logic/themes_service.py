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


# --- Upload size limits (SC-13) -------------------------------------------------
# A theme is two small JSON files plus a few background images. Without limits a
# few-MB ZIP could expand to gigabytes on the storage shared by every instance.
MAX_THEME_JSON_BYTES = 2 * 1024 * 1024
MAX_THEME_ASSET_BYTES = 8 * 1024 * 1024
MAX_THEME_ZIP_ENTRIES = 200


def max_theme_zip_bytes() -> int:
    """Upper bound for an uploaded theme ZIP (``LYNDRIX_THEME_MAX_ZIP_MB``)."""
    from config import settings

    try:
        megabytes = int(getattr(settings, "LYNDRIX_THEME_MAX_ZIP_MB", 50) or 50)
    except (TypeError, ValueError):
        megabytes = 50
    return (megabytes if megabytes > 0 else 50) * 1024 * 1024


def check_zip_limits(zf, upload_size: int) -> None:
    """Refuse an oversized theme ZIP before anything is read or extracted.

    ``upload_size`` is the size of the uploaded file; the entries are judged by
    their declared (uncompressed) sizes, which bound what ``zipfile`` extracts.
    Raises ``ThemeRejected``.
    """
    limit = max_theme_zip_bytes()
    megabytes = limit // (1024 * 1024)
    if upload_size > limit:
        raise ThemeRejected(f"Theme ZIP is larger than {megabytes} MB")
    entries = zf.infolist()
    if len(entries) > MAX_THEME_ZIP_ENTRIES:
        raise ThemeRejected(f"Theme ZIP has more than {MAX_THEME_ZIP_ENTRIES} entries")
    for name in ("tokens.json", "components.json"):
        try:
            size = zf.getinfo(name).file_size
        except KeyError:
            continue  # reported as "missing" by the caller
        if size > MAX_THEME_JSON_BYTES:
            raise ThemeRejected(
                f"{name} is larger than {MAX_THEME_JSON_BYTES // (1024 * 1024)} MB"
            )
    if sum(entry.file_size for entry in entries) > limit:
        raise ThemeRejected(f"Theme ZIP would extract to more than {megabytes} MB")


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
