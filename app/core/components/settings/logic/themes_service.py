"""Active-theme selection — one path shared by the REST API and NiceGUI.

The active theme is just the ``DEFAULT_THEME_ID`` key in ``core/settings``, so this
delegates persistence to ``system_config_service`` and adds the theme-specific
concerns: validating the theme exists and emitting ``ui:needs_refresh``.
"""
from __future__ import annotations

from core.logger import get_logger

log = get_logger("Core:ThemesService")


class ThemeRejected(ValueError):
    """An uploaded theme pack is malformed, too large or carries unsafe values.

    ``status_code`` is the HTTP status the REST API answers with.
    """

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


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
        raise ThemeRejected(f"Theme ZIP is larger than {megabytes} MB", 413)
    entries = zf.infolist()
    if len(entries) > MAX_THEME_ZIP_ENTRIES:
        raise ThemeRejected(f"Theme ZIP has more than {MAX_THEME_ZIP_ENTRIES} entries", 413)
    for name in ("tokens.json", "components.json"):
        try:
            size = zf.getinfo(name).file_size
        except KeyError:
            continue  # reported as "missing" by the caller
        if size > MAX_THEME_JSON_BYTES:
            raise ThemeRejected(
                f"{name} is larger than {MAX_THEME_JSON_BYTES // (1024 * 1024)} MB", 413
            )
    if sum(entry.file_size for entry in entries) > limit:
        raise ThemeRejected(f"Theme ZIP would extract to more than {megabytes} MB", 413)


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


# Image types a theme may ship in ``assets/`` (lowercase suffix → media type).
THEME_IMAGE_MEDIA_TYPES = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def theme_id_from_filename(filename: str) -> str:
    """Derive and validate the theme id from the uploaded file name."""
    from pathlib import PurePosixPath

    from core.theming.loader import _THEME_ID_RE

    theme_id = PurePosixPath((filename or "").replace("\\", "/")).stem
    if not _THEME_ID_RE.fullmatch(theme_id or ""):
        raise ThemeRejected("Invalid theme ID derived from filename")
    if theme_id == "default":
        # The built-in default is the fallback of every other theme.
        raise ThemeRejected("Cannot overwrite the default theme", 400)
    return theme_id


def install_theme_zip(filename: str, data: bytes) -> tuple[str, list[str]]:
    """Validate an uploaded theme ZIP and store it on the shared storage.

    The one upload path for the REST API and the NiceGUI settings page:
    id from the file name (never ``default``), size limits, strict value
    validation, then extraction into ``STORAGE_DIR/themes/<id>`` — visible to
    every cluster instance and kept across image updates; the built-in themes
    inside the image are never written. Returns ``(theme_id, warnings)``;
    raises ``ThemeRejected``. Blocking (file IO) — call via ``asyncio.to_thread``.
    """
    import io
    import shutil
    import zipfile
    from pathlib import Path

    from core.theming import get_theme_engine
    from core.theming.loader import load_theme_pack
    from core.theming.schema import validate_theme_pack

    theme_id = theme_id_from_filename(filename)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ThemeRejected("Invalid ZIP file") from exc

    with archive as zf:
        check_zip_limits(zf, len(data))
        names = zf.namelist()
        if "tokens.json" not in names or "components.json" not in names:
            raise ThemeRejected("ZIP must contain tokens.json and components.json at root level")
        # Validate BEFORE anything is written: a rejected upload must not
        # replace (or half-replace) an existing theme of the same id, and an
        # unsafe pack must never sit on the shared storage (SC-02).
        check_uploaded_pack(theme_id, zf.read("tokens.json"), zf.read("components.json"))

        user_dir = get_theme_engine().user_dir
        dest = user_dir / theme_id
        dest.mkdir(parents=True, exist_ok=True)
        zf.extract("tokens.json", dest)
        zf.extract("components.json", dest)

        # Image assets at ``assets/<name>.{png,jpg,jpeg,webp}`` only — a single
        # safe path component, per-file size capped; everything else is skipped.
        assets_dir = dest / "assets"
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.replace("\\", "/").split("/")
            if len(parts) != 2 or parts[0] != "assets":
                continue
            basename = parts[1]
            if not basename or basename.startswith(".") or ".." in basename:
                continue
            if Path(basename).suffix.lower() not in THEME_IMAGE_MEDIA_TYPES:
                continue
            if info.file_size > MAX_THEME_ASSET_BYTES:
                continue
            assets_dir.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, (assets_dir / basename).open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 64)

    try:
        warnings = validate_theme_pack(load_theme_pack(user_dir, theme_id))
    except Exception as exc:
        raise ThemeRejected(f"Theme validation failed: {exc}") from exc
    return theme_id, warnings
