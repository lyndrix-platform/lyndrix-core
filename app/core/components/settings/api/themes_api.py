import io
import json
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from core.api.security import ApiIdentity, require_permission
from core.logger import get_logger

log = get_logger("Core:ThemesAPI")

themes_router = APIRouter(prefix="/api/themes", tags=["Themes"])

_THEMES_BASE_DIR = Path(__file__).resolve().parents[4] / "assets" / "themes"


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
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    theme_dir = _THEMES_BASE_DIR / payload.theme_id
    if not theme_dir.exists():
        raise HTTPException(status_code=404, detail=f"Theme '{payload.theme_id}' not found")

    from config import settings
    settings.DEFAULT_THEME_ID = payload.theme_id

    try:
        from core.services import vault_instance
        if vault_instance.is_connected:
            existing: dict = {}
            try:
                resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                    path="core/settings", mount_point="lyndrix"
                )
                existing = resp["data"]["data"] or {}
            except Exception:
                pass
            existing["DEFAULT_THEME_ID"] = payload.theme_id
            vault_instance.client.secrets.kv.v2.create_or_update_secret(
                path="core/settings", mount_point="lyndrix", secret=existing
            )
    except Exception as exc:
        log.warning(f"API: Could not persist active theme to Vault: {exc}")

    from core.bus import bus
    bus.emit("ui:needs_refresh", {"reason": "theme_changed"})
    log.info(f"API: Active theme set to '{payload.theme_id}' by '{identity.username}'.")
    return {"status": "ok", "theme_id": payload.theme_id}


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
