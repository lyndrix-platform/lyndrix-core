import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.api.security import ApiIdentity, require_permission
from core.logger import get_logger
from core.theming.loader import _THEME_ID_RE

log = get_logger("Core:ThemesAPI")

themes_router = APIRouter(prefix="/api/themes", tags=["Themes"])

def _engine():
    from core.theming import get_theme_engine

    return get_theme_engine()


def _theme_dir(theme_id: str) -> Path:
    """Built-in (image) or uploaded (shared ``STORAGE_DIR/themes``) theme directory.

    404 for anything but a single safe id — the engine validates against
    ``[A-Za-z0-9_-]+``, so a traversal attempt never touches the filesystem."""
    if not _is_safe_component(theme_id):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return _engine().theme_dir(theme_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")

# Allowed image extensions for theme background assets (lowercase) → media type.
# Shared with the upload path so what can be uploaded is what can be served.
from core.components.settings.logic.themes_service import (  # noqa: E402
    THEME_IMAGE_MEDIA_TYPES as _IMAGE_MEDIA_TYPES,
)


def _is_safe_component(name: str) -> bool:
    """True if ``name`` is a single safe path component (no traversal)."""
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name and not name.startswith(".")


class ActiveThemeRequest(BaseModel):
    theme_id: str


@themes_router.get("", summary="List available themes")
async def list_themes(identity: ApiIdentity = Depends(require_permission("api:read"))):
    from core.theming import get_theme_engine
    themes = get_theme_engine().list_available_themes()
    return {"status": "ok", "themes": themes}


# NOTE: /active must be declared before /{theme_id} so it is matched first.
@themes_router.get("/active", summary="Get the active theme ID")
async def get_active_theme(identity: ApiIdentity = Depends(require_permission("api:read"))):
    from config import settings
    return {"status": "ok", "theme_id": settings.DEFAULT_THEME_ID or "default"}


@themes_router.put("/active", summary="Set the active theme")
async def set_active_theme(
    payload: ActiveThemeRequest,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    from core.components.settings.logic import themes_service

    try:
        themes_service.set_active(payload.theme_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    log.info(f"API: Active theme set to '{payload.theme_id}' by '{identity.username}'.")
    return {"status": "ok", "theme_id": payload.theme_id}


# NOTE: declared before /{theme_id} so the unambiguous sub-path matches first.
# Unauthenticated on purpose: background images are referenced by the browser
# through CSS url(...) which cannot carry an Authorization header (mirrors the
# i18n catalog / static plugin bundles). Theme assets are not secret.
@themes_router.get("/{theme_id}/assets/{filename}", summary="Serve a theme asset")
async def get_theme_asset(theme_id: str, filename: str):
    if not _is_safe_component(theme_id) or not _is_safe_component(filename):
        raise HTTPException(status_code=404, detail="Not found")

    ext = Path(filename).suffix.lower()
    media_type = _IMAGE_MEDIA_TYPES.get(ext)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Not found")

    asset_path = _theme_dir(theme_id) / "assets" / filename
    if not asset_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    stat = asset_path.stat()
    etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
    return FileResponse(
        asset_path,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": etag,
        },
    )


# NOTE: declared before /{theme_id} so the unambiguous sub-path matches first.
@themes_router.get("/{theme_id}/css-vars", summary="Get resolved --lx-* CSS variables")
async def get_theme_css_vars(
    theme_id: str,
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    if not _engine().theme_exists(theme_id):
        raise HTTPException(status_code=404, detail=f"Theme '{theme_id}' not found")
    engine = _engine()
    return {
        "status": "ok",
        "theme_id": theme_id,
        "css_variables": {
            "light": engine.resolve_css_variables(False, theme_id),
            "dark": engine.resolve_css_variables(True, theme_id),
        },
    }


@themes_router.get("/{theme_id}", summary="Get theme tokens and components")
async def get_theme_detail(
    theme_id: str,
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    theme_dir = _theme_dir(theme_id)
    if not theme_dir.exists():
        raise HTTPException(status_code=404, detail=f"Theme '{theme_id}' not found")
    try:
        tokens = json.loads((theme_dir / "tokens.json").read_text())
        components = json.loads((theme_dir / "components.json").read_text())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read theme files: {exc}")
    return {"status": "ok", "theme_id": theme_id, "tokens": tokens, "components": components}


@themes_router.post("", summary="Upload a theme ZIP", status_code=201)
async def upload_theme(
    file: UploadFile = File(...),
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    if not file.filename:
        raise HTTPException(status_code=422, detail="No file provided")

    from core.components.settings.logic import themes_service

    # Read at most one byte beyond the limit: enough to know it is too large
    # without buffering an arbitrarily big upload (SC-13).
    limit = themes_service.max_theme_zip_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413, detail=f"Theme ZIP is larger than {limit // (1024 * 1024)} MB"
        )
    try:
        # Validation + extraction are blocking file IO — off the event loop.
        theme_id, warnings = await asyncio.to_thread(
            themes_service.install_theme_zip, file.filename, data
        )
    except themes_service.ThemeRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    log.info(f"API: Theme '{theme_id}' uploaded by '{identity.username}' (warnings={len(warnings)}).")
    return {"status": "ok", "theme_id": theme_id, "validation_warnings": warnings}


@themes_router.delete("/{theme_id}", summary="Delete a theme")
async def delete_theme(
    theme_id: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    if theme_id == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default theme")
    if not _is_safe_component(theme_id) or not _THEME_ID_RE.match(theme_id):
        raise HTTPException(status_code=404, detail=f"Theme '{theme_id}' not found")

    dest = _engine().user_dir / theme_id
    if not dest.exists():
        if _engine().theme_exists(theme_id):
            raise HTTPException(status_code=400, detail="Built-in themes cannot be deleted")
        raise HTTPException(status_code=404, detail=f"Theme '{theme_id}' not found")

    shutil.rmtree(dest)
    log.info(f"API: Theme '{theme_id}' deleted by '{identity.username}'.")
    return {"status": "ok", "deleted": theme_id}
