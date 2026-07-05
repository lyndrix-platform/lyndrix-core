import io
import json
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.api.security import ApiIdentity, require_permission
from core.logger import get_logger

log = get_logger("Core:ThemesAPI")

themes_router = APIRouter(prefix="/api/themes", tags=["Themes"])

_THEMES_BASE_DIR = Path(__file__).resolve().parents[4] / "assets" / "themes"

# Allowed image extensions for theme background assets (lowercase) → media type.
_IMAGE_MEDIA_TYPES = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# Per-image size cap for ZIP-uploaded assets (~8 MB).
_MAX_ASSET_BYTES = 8 * 1024 * 1024


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

    asset_path = _THEMES_BASE_DIR / theme_id / "assets" / filename
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
    theme_dir = _THEMES_BASE_DIR / theme_id
    if not theme_dir.exists():
        raise HTTPException(status_code=404, detail=f"Theme '{theme_id}' not found")
    from core.theming import get_theme_engine

    engine = get_theme_engine()
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
    theme_dir = _THEMES_BASE_DIR / theme_id
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

    theme_id = Path(file.filename).stem
    if not theme_id or "/" in theme_id or theme_id.startswith("."):
        raise HTTPException(status_code=422, detail="Invalid theme ID derived from filename")
    if theme_id == "default":
        raise HTTPException(status_code=400, detail="Cannot overwrite the default theme")

    data = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "tokens.json" not in names or "components.json" not in names:
                raise HTTPException(
                    status_code=422,
                    detail="ZIP must contain tokens.json and components.json at root level",
                )
            dest = _THEMES_BASE_DIR / theme_id
            dest.mkdir(parents=True, exist_ok=True)
            zf.extract("tokens.json", dest)
            zf.extract("components.json", dest)

            # Also extract any image assets at `assets/<name>.{png,jpg,jpeg,webp}`
            # into dest/assets/<name>. Traversal-guarded + per-file size capped.
            assets_dir = dest / "assets"
            for info in zf.infolist():
                if info.is_dir():
                    continue
                entry = info.filename
                # Reject absolute paths and any traversal attempt outright.
                if entry.startswith("/") or ".." in entry.replace("\\", "/").split("/"):
                    continue
                parts = entry.replace("\\", "/").split("/")
                # Only a single `assets/` prefix + safe basename is allowed.
                if len(parts) != 2 or parts[0] != "assets":
                    continue
                basename = parts[1]
                if not _is_safe_component(basename):
                    continue
                if Path(basename).suffix.lower() not in _IMAGE_MEDIA_TYPES:
                    continue
                if info.file_size > _MAX_ASSET_BYTES:
                    continue
                assets_dir.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, (assets_dir / basename).open("wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 64)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="Invalid ZIP file")

    try:
        from core.theming.schema import validate_theme_pack
        from core.theming.loader import load_theme_pack
        pack = load_theme_pack(_THEMES_BASE_DIR, theme_id)
        errors = validate_theme_pack(pack)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Theme validation failed: {exc}")

    log.info(f"API: Theme '{theme_id}' uploaded by '{identity.username}' (warnings={len(errors)}).")
    return {"status": "ok", "theme_id": theme_id, "validation_warnings": errors}


@themes_router.delete("/{theme_id}", summary="Delete a theme")
async def delete_theme(
    theme_id: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    if theme_id == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default theme")

    dest = _THEMES_BASE_DIR / theme_id
    if not dest.exists():
        raise HTTPException(status_code=404, detail=f"Theme '{theme_id}' not found")

    shutil.rmtree(dest)
    log.info(f"API: Theme '{theme_id}' deleted by '{identity.username}'.")
    return {"status": "ok", "deleted": theme_id}
