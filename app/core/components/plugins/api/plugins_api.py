from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api.security import ApiIdentity, require_permission
from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:PluginsAPI")

plugins_router = APIRouter(prefix="/api/plugins", tags=["Plugins"])


class PluginOut(BaseModel):
    id: str
    name: str
    version: str
    description: Optional[str] = None
    type: str
    is_active: bool
    repo_url: Optional[str] = None
    ui_route: Optional[str] = None
    status: str


class InstallRequest(BaseModel):
    url: str
    version: str = "latest"


class UpgradeRequest(BaseModel):
    version: str = "latest"


class CustomRepoRequest(BaseModel):
    repo_url: str
    name: Optional[str] = None
    description: Optional[str] = None
    token: Optional[str] = None


def _manager():
    from core.components.plugins.logic.manager import module_manager
    return module_manager


def _plugin_service():
    from core.components.plugins.logic.plugin_service import plugin_service
    return plugin_service


def _custom_repo_service():
    from core.components.plugins.logic.custom_repo_service import custom_repo_service
    return custom_repo_service


def _to_plugin_out(module_id: str, entry: dict) -> PluginOut:
    manifest = entry["manifest"]
    return PluginOut(
        id=module_id,
        name=manifest.name,
        version=manifest.version,
        description=getattr(manifest, "description", None),
        type=manifest.type,
        is_active=entry.get("status") == "active",
        repo_url=getattr(manifest, "repo_url", None),
        ui_route=getattr(manifest, "ui_route", None),
        status=entry.get("status", "unknown"),
    )


@plugins_router.get("", summary="List all loaded plugins and core modules")
async def list_plugins(identity: ApiIdentity = Depends(require_permission("api:read"))):
    mgr = _manager()
    plugins = [
        _to_plugin_out(mid, entry).model_dump()
        for mid, entry in mgr.registry.items()
    ]
    return {"status": "ok", "count": len(plugins), "plugins": plugins}


@plugins_router.post("/{plugin_id}/enable", summary="Enable a plugin")
async def enable_plugin(
    plugin_id: str,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    mgr = _manager()
    if plugin_id not in mgr.registry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    ok = mgr.toggle_module(plugin_id, True)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to enable '{plugin_id}'")
    bus.emit("plugin:state_changed", {"plugin_id": plugin_id, "is_active": True})
    log.info(f"API: Plugin '{plugin_id}' enabled by '{identity.username}'.")
    return {"status": "ok", "plugin_id": plugin_id, "is_active": True}


@plugins_router.post("/{plugin_id}/disable", summary="Disable a plugin")
async def disable_plugin(
    plugin_id: str,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    mgr = _manager()
    if plugin_id not in mgr.registry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    ok = mgr.toggle_module(plugin_id, False)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to disable '{plugin_id}'")
    bus.emit("plugin:state_changed", {"plugin_id": plugin_id, "is_active": False})
    log.info(f"API: Plugin '{plugin_id}' disabled by '{identity.username}'.")
    return {"status": "ok", "plugin_id": plugin_id, "is_active": False}


@plugins_router.post("/{plugin_id}/reload", summary="Reload a plugin in-place")
async def reload_plugin(
    plugin_id: str,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    mgr = _manager()
    if plugin_id not in mgr.registry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    bus.emit("plugin:reload_requested", {"plugin_id": plugin_id})
    log.info(f"API: Reload requested for '{plugin_id}' by '{identity.username}'.")
    return {"status": "ok", "plugin_id": plugin_id, "action": "reload_requested"}


@plugins_router.post("/install", summary="Install a plugin from a GitHub/Git URL")
async def install_plugin(
    payload: InstallRequest,
    background_tasks: BackgroundTasks,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    svc = _plugin_service()
    log.info(f"API: Install queued for '{payload.url}' v{payload.version} by '{identity.username}'.")
    background_tasks.add_task(svc.install_plugin, payload.url, payload.version, False)
    return {"status": "ok", "action": "install_queued", "url": payload.url, "version": payload.version}


@plugins_router.post("/{plugin_id}/upgrade", summary="Upgrade a plugin to a specific version")
async def upgrade_plugin(
    plugin_id: str,
    payload: UpgradeRequest,
    background_tasks: BackgroundTasks,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    mgr = _manager()
    if plugin_id not in mgr.registry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    manifest = mgr.registry[plugin_id]["manifest"]
    repo_url = getattr(manifest, "repo_url", None)
    if not repo_url:
        raise HTTPException(status_code=400, detail=f"Plugin '{plugin_id}' has no repo_url")
    svc = _plugin_service()
    log.info(f"API: Upgrade queued for '{plugin_id}' v{payload.version} by '{identity.username}'.")
    background_tasks.add_task(svc.install_plugin, repo_url, payload.version, True)
    return {"status": "ok", "action": "upgrade_queued", "plugin_id": plugin_id, "version": payload.version}


@plugins_router.post("/{plugin_id}/uninstall", summary="Uninstall a plugin")
async def uninstall_plugin(
    plugin_id: str,
    background_tasks: BackgroundTasks,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    import pathlib

    mgr = _manager()
    if plugin_id not in mgr.registry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")

    entry = mgr.registry[plugin_id]
    manifest = entry["manifest"]
    if manifest.type != "PLUGIN":
        raise HTTPException(status_code=400, detail="Cannot uninstall core modules")

    module = entry.get("module")
    if module and hasattr(module, "__file__") and module.__file__:
        repo_name = pathlib.Path(module.__file__).parent.name
    else:
        repo_name = plugin_id.split(".")[-1]

    svc = _plugin_service()
    log.info(f"API: Uninstall queued for '{plugin_id}' by '{identity.username}'.")
    background_tasks.add_task(svc.uninstall_plugin, plugin_id, repo_name)
    return {"status": "ok", "action": "uninstall_queued", "plugin_id": plugin_id}


@plugins_router.get("/marketplace", summary="Fetch the plugin marketplace listing")
async def get_marketplace(
    force_refresh: bool = False,
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    svc = _plugin_service()
    plugins = await svc.fetch_marketplace_data(force_refresh=force_refresh)
    return {"status": "ok", "count": len(plugins), "plugins": plugins}


@plugins_router.get("/versions", summary="Get available versions for a plugin URL")
async def get_plugin_versions(
    url: str,
    force_refresh: bool = False,
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    svc = _plugin_service()
    versions = await svc.get_plugin_versions(url, force_refresh=force_refresh)
    return {"status": "ok", "url": url, "versions": versions}


# ── Custom repositories ──────────────────────────────────────────────────────

@plugins_router.get("/custom-repos", summary="List custom plugin repositories")
async def list_custom_repos(identity: ApiIdentity = Depends(require_permission("api:read"))):
    repos = _custom_repo_service().list_all()
    return {
        "status": "ok",
        "count": len(repos),
        "repos": [
            {
                "id": r.id,
                "name": r.name,
                "repo_url": r.repo_url,
                "provider": r.provider,
                "description": r.description,
                "enabled": r.enabled,
                "has_token": r.has_token,
            }
            for r in repos
        ],
    }


@plugins_router.post("/custom-repos", summary="Add a custom plugin repository", status_code=201)
async def add_custom_repo(
    payload: CustomRepoRequest,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    try:
        row = _custom_repo_service().create(
            repo_url=payload.repo_url,
            name=payload.name,
            description=payload.description,
            token=payload.token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    log.info(f"API: Custom repo '{row.name}' added by '{identity.username}'.")
    return {
        "status": "ok",
        "repo": {
            "id": row.id,
            "name": row.name,
            "repo_url": row.repo_url,
            "provider": row.provider,
            "description": row.description,
            "enabled": row.enabled,
            "has_token": row.has_token,
        },
    }


@plugins_router.delete("/custom-repos/{repo_id}", summary="Remove a custom plugin repository")
async def delete_custom_repo(
    repo_id: int,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    ok = _custom_repo_service().delete(repo_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Custom repo {repo_id} not found")
    log.info(f"API: Custom repo {repo_id} removed by '{identity.username}'.")
    return {"status": "ok", "deleted": repo_id}
