import io
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from core.api.security import ApiIdentity, require_permission
from core.logger import get_logger

log = get_logger("Core:ThemesAPI")

themes_router = APIRouter(prefix="/api/themes", tags=["Themes"])

_THEMES_BASE_DIR = Path(__file__).resolve().parents[4] / "assets" / "themes"


@themes_router.get("", summary="List available themes")
async def list_themes(identity: ApiIdentity = Depends(require_permission("api:read"))):
    from core.theming import get_theme_engine
    themes = get_theme_engine().list_available_themes()
    return {"status": "ok", "themes": themes}


@themes_router.post("", summary="Upload a theme ZIP", status_code=201)
async def upload_theme(
    file: UploadFile = File(...),
    identity: ApiIdentity = Depends(require_permission("api:write")),
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
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    if theme_id == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default theme")

    dest = _THEMES_BASE_DIR / theme_id
    if not dest.exists():
        raise HTTPException(status_code=404, detail=f"Theme '{theme_id}' not found")

    shutil.rmtree(dest)
    log.info(f"API: Theme '{theme_id}' deleted by '{identity.username}'.")
    return {"status": "ok", "deleted": theme_id}
