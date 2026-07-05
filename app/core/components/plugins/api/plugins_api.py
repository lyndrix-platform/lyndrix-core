from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

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
    icon: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    react_ui: bool = False
    react_routes: List[dict] = []
    settings_schema: List[dict] = []
    settings_ui_route: Optional[str] = None
    i18n_namespace: Optional[str] = None
    # WS-2.B: in-memory update-check result (no network cost per request).
    latest_version: Optional[str] = None
    update_available: bool = False
    # Precomputed logger name for GET /api/logs (convention stays server-side).
    log_source: str = ""


class PluginSettingsUpdate(BaseModel):
    values: Dict[str, Any]


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
    latest, update_available = _plugin_service().latest_version_info(
        module_id, manifest.version
    )
    return PluginOut(
        id=module_id,
        name=manifest.name,
        version=manifest.version,
        description=getattr(manifest, "description", None),
        type=manifest.type,
        is_active=entry.get("status") == "active",
        repo_url=getattr(manifest, "repo_url", None),
        ui_route=getattr(manifest, "ui_route", None),
        icon=getattr(manifest, "icon", None),
        status=entry.get("status", "unknown"),
        error_message=entry.get("error_message"),
        react_ui=getattr(manifest, "react_ui", False),
        react_routes=getattr(manifest, "react_routes", []),
        settings_schema=[f.model_dump() for f in getattr(manifest, "settings_schema", [])],
        settings_ui_route=getattr(manifest, "settings_ui_route", None),
        i18n_namespace=getattr(manifest, "i18n_namespace", None),
        latest_version=latest,
        update_available=update_available,
        log_source=f"{'Core' if manifest.type == 'CORE' else 'Plugin'}:{manifest.name}",
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
    identity: ApiIdentity = Depends(require_permission("admin:write")),
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
    identity: ApiIdentity = Depends(require_permission("admin:write")),
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
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    mgr = _manager()
    if plugin_id not in mgr.registry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    # Reload synchronously so the response reflects the real outcome. The old
    # implementation emitted plugin:reload_requested, which had no subscriber —
    # the button was a no-op.
    log.info(f"API: Reload requested for '{plugin_id}' by '{identity.username}'.")
    ok = await mgr.reload_module(plugin_id)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Reload of '{plugin_id}' failed — see server logs.")
    bus.emit("plugin:state_changed", {"plugin_id": plugin_id, "action": "reloaded"})
    return {"status": "ok", "plugin_id": plugin_id, "action": "reloaded"}


@plugins_router.post("/install", summary="Install a plugin from a GitHub/Git URL")
async def install_plugin(
    payload: InstallRequest,
    background_tasks: BackgroundTasks,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    svc = _plugin_service()
    # Validate the repo host against the configured allowlist synchronously so
    # a rejected URL surfaces as 422 to the caller — install runs as a
    # background task afterwards, where a raised error would only be logged.
    try:
        svc.validate_repo_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    log.info(f"API: Install queued for '{payload.url}' v{payload.version} by '{identity.username}'.")
    background_tasks.add_task(svc.install_plugin, payload.url, payload.version, False)
    return {"status": "ok", "action": "install_queued", "url": payload.url, "version": payload.version}


@plugins_router.post("/{plugin_id}/upgrade", summary="Upgrade a plugin to a specific version")
async def upgrade_plugin(
    plugin_id: str,
    payload: UpgradeRequest,
    background_tasks: BackgroundTasks,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    mgr = _manager()
    if plugin_id not in mgr.registry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    manifest = mgr.registry[plugin_id]["manifest"]
    repo_url = getattr(manifest, "repo_url", None)
    if not repo_url:
        raise HTTPException(status_code=400, detail=f"Plugin '{plugin_id}' has no repo_url")
    svc = _plugin_service()
    try:
        svc.validate_repo_url(repo_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    log.info(f"API: Upgrade queued for '{plugin_id}' v{payload.version} by '{identity.username}'.")
    background_tasks.add_task(svc.install_plugin, repo_url, payload.version, True)
    return {"status": "ok", "action": "upgrade_queued", "plugin_id": plugin_id, "version": payload.version}


@plugins_router.post("/{plugin_id}/uninstall", summary="Uninstall a plugin")
async def uninstall_plugin(
    plugin_id: str,
    background_tasks: BackgroundTasks,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
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
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    svc = _plugin_service()
    plugins = await svc.fetch_marketplace_data(force_refresh=force_refresh)
    return {"status": "ok", "count": len(plugins), "plugins": plugins}


@plugins_router.get("/versions", summary="Get available versions for a plugin URL")
async def get_plugin_versions(
    url: str,
    force_refresh: bool = False,
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    svc = _plugin_service()
    try:
        versions = await svc.get_plugin_versions(url, force_refresh=force_refresh)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", "url": url, "versions": versions}


# ── Custom repositories ──────────────────────────────────────────────────────

@plugins_router.get("/custom-repos", summary="List custom plugin repositories")
async def list_custom_repos(identity: ApiIdentity = Depends(require_permission("admin:read"))):
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
    identity: ApiIdentity = Depends(require_permission("admin:write")),
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
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    ok = _custom_repo_service().delete(repo_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Custom repo {repo_id} not found")
    log.info(f"API: Custom repo {repo_id} removed by '{identity.username}'.")
    return {"status": "ok", "deleted": repo_id}


@plugins_router.get("/{plugin_id}/settings", summary="Get plugin settings schema and current values")
async def get_plugin_settings(
    plugin_id: str,
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    from core.components.vault.logic.kv_helper import read_secret_dict

    mgr = _manager()
    entry = mgr.registry.get(plugin_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")

    manifest = entry["manifest"]
    schema_fields = getattr(manifest, "settings_schema", [])
    schema = [f.model_dump() for f in schema_fields]

    # read_secret_dict returns {} for "no settings yet" and logs real Vault errors.
    values: Dict[str, Any] = read_secret_dict(f"plugins/{plugin_id}/settings")

    for field in schema_fields:
        if field.key not in values and field.default is not None:
            values[field.key] = field.default

    # Never return stored secret values verbatim. Mask them and tell the UI which
    # keys currently hold a value (so it can render "set/unset" without leaking it).
    secret_keys = [f.key for f in schema_fields if f.kind == "secret"]
    secret_values_set = {}
    for key in secret_keys:
        has_value = bool(str(values.get(key) or "").strip())
        secret_values_set[key] = has_value
        # Drop the real value from the response entirely.
        values[key] = ""

    return {
        "status": "ok",
        "plugin_id": plugin_id,
        "schema": schema,
        "values": values,
        "secret_keys": secret_keys,
        "secret_values_set": secret_values_set,
    }


@plugins_router.put("/{plugin_id}/settings", summary="Update plugin settings")
async def update_plugin_settings(
    plugin_id: str,
    payload: PluginSettingsUpdate,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    from core.components.vault.logic.kv_helper import read_secret_dict, write_secret_dict

    mgr = _manager()
    entry = mgr.registry.get(plugin_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")

    manifest = entry["manifest"]
    schema_fields = getattr(manifest, "settings_schema", [])
    schema_by_key = {f.key: f for f in schema_fields}

    # Existing stored values — used to preserve secrets that are submitted blank
    # (the GET masks them, so a round-trip without retyping must not wipe them).
    existing: Dict[str, Any] = read_secret_dict(f"plugins/{plugin_id}/settings")

    coerced: Dict[str, Any] = {}
    for key, raw in payload.values.items():
        if key not in schema_by_key:
            raise HTTPException(status_code=400, detail=f"Unknown setting key: '{key}'")
        field = schema_by_key[key]
        # Reject structured values for every scalar field kind — a dict/list would
        # otherwise be stringified to a Python repr and silently corrupt the value.
        if isinstance(raw, (dict, list)):
            raise HTTPException(status_code=400, detail=f"'{key}' must be a scalar value, not an object/array")
        if field.kind == "secret":
            new_val = "" if raw is None else str(raw)
            # Blank submission means "unchanged" — keep the stored secret.
            coerced[key] = new_val if new_val.strip() else existing.get(key, "")
            continue
        if field.kind == "bool":
            coerced[key] = bool(raw) if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
        elif field.kind == "int":
            try:
                coerced[key] = int(raw)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"'{key}' must be an integer")
        elif field.kind == "select":
            if field.options and str(raw) not in field.options:
                raise HTTPException(status_code=400, detail=f"'{key}' must be one of {field.options}")
            coerced[key] = str(raw)
        else:
            coerced[key] = "" if raw is None else str(raw)

    if not write_secret_dict(f"plugins/{plugin_id}/settings", coerced):
        raise HTTPException(status_code=503, detail="Vault unavailable — settings were not persisted")

    log.info(f"API: Settings updated for '{plugin_id}' by '{identity.username}'.")
    return {"status": "ok", "plugin_id": plugin_id, "saved": list(coerced.keys())}


@plugins_router.post("/{plugin_id}/rollback", summary="Roll a plugin back to its previous version")
async def rollback_plugin(
    plugin_id: str,
    identity: ApiIdentity = Depends(require_permission("admin:write")),
):
    import pathlib

    mgr = _manager()
    entry = mgr.registry.get(plugin_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    if entry["manifest"].type != "PLUGIN":
        raise HTTPException(status_code=400, detail="Cannot roll back core modules")

    module = entry.get("module")
    if module and getattr(module, "__file__", None):
        repo_name = pathlib.Path(module.__file__).parent.name
    else:
        repo_name = plugin_id.split(".")[-1]

    svc = _plugin_service()
    if not svc.has_previous(repo_name):
        raise HTTPException(status_code=409, detail="No previous version to roll back to")

    ok = await svc.rollback_plugin(plugin_id, repo_name)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Rollback failed for '{plugin_id}'")

    log.info(f"API: Plugin '{plugin_id}' rolled back by '{identity.username}'.")
    return {"status": "ok", "plugin_id": plugin_id, "action": "rolled_back"}


# NOTE: This route must be declared AFTER all specific sub-paths (marketplace, versions,
# custom-repos, settings, rollback) so FastAPI matches those literals before falling through to {plugin_id}.
@plugins_router.get("/{plugin_id}", summary="Get plugin details")
async def get_plugin(
    plugin_id: str,
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    mgr = _manager()
    entry = mgr.registry.get(plugin_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    out = _to_plugin_out(plugin_id, entry).model_dump()
    out["manifest"] = entry["manifest"].model_dump()
    return {"status": "ok", "plugin": out}
