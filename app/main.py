from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from nicegui import ui
from pydantic import BaseModel, ValidationError

from config import Settings, settings
from core.bus import bus
from core.logger import setup_logging, get_logger
from core.session import is_authenticated

# --- FIX: Load exclusively from the facade ---
from core.services import vault_instance, boot_service

# --- Global API surface ---
from core.api import __api_version__, __core_version__, router_registry
from core.api import PluginHealthStatus
from core.api import (
    ApiIdentity,
    optional_api_auth,
    require_permission,
    system_api_key_configured,
)
from core.api.permissions_api import permissions_router
from core.components.sockets.api.socket_api import socket_router
from core.components.sockets.providers.docker_provider import DockerProvider
from core.components.sockets.registry import get_registry

# --- Route Registrations ---
from core.components.auth.ui.routes import (
    register_auth_routes,
    register_oidc_fastapi_routes,
)
from core.components.settings.ui.routes import register_settings_routes
from core.components.vault.ui.routes import register_vault_routes
from core.components.dashboard.ui.routes import register_dashboard_routes

# --- Global UI ---
from ui.theme import apply_theme
from ui.maintenance import attach_maintenance_overlay
from version import __version__

setup_logging()

# Register core socket providers
registry = get_registry()
registry.register(DockerProvider, plugin_name="core")

app = FastAPI(
    title="Lyndrix Core API",
    description="Core and plugin endpoints for Lyndrix.",
    version=__version__,
    docs_url=None,
    redoc_url=None,
)
log = get_logger("Core:Main")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

_SWAGGER_DARK_CSS = f"/assets/vendor/swagger-dark.css?v={__version__}"


def _custom_openapi():
    """Augment the OpenAPI schema with auth security schemes.

    Declaring these makes Swagger UI render the **Authorize** button so callers
    can paste an API key (or Basic credentials) and try authenticated endpoints
    directly from the docs. The schemes mirror the methods accepted by
    ``core.api.security``: a system/per-user API key (``X-API-Key`` header or
    ``Bearer`` token) and HTTP Basic against the local IAM.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(
        {
            "ApiKeyHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "System API key or per-user API key (prefix `lyk_`).",
            },
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Send the API key as a Bearer token.",
            },
            "BasicAuth": {
                "type": "http",
                "scheme": "basic",
                "description": "Dashboard username and password (local IAM).",
            },
        }
    )
    # Offered globally so the Authorize button applies the chosen credential to
    # every Try-it-out request. Public endpoints simply ignore it server-side.
    schema["security"] = [
        {"ApiKeyHeader": []},
        {"BearerAuth": []},
        {"BasicAuth": []},
    ]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """Swagger UI with the Lyndrix dark theme and an Authorize button."""
    response = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} — Docs",
        swagger_favicon_url=f"/assets/icons/favicon.ico?v={__version__}",
        swagger_ui_parameters={"persistAuthorization": True, "tryItOutEnabled": True},
    )
    body = response.body.decode("utf-8")
    body = body.replace(
        "</head>",
        f'<link type="text/css" rel="stylesheet" href="{_SWAGGER_DARK_CSS}">\n</head>',
    )
    return HTMLResponse(body)


@app.get("/redoc", include_in_schema=False)
async def custom_redoc_html():
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} — ReDoc",
        redoc_favicon_url=f"/assets/icons/favicon.ico?v={__version__}",
    )


def _safe_is_authenticated() -> bool:
    return is_authenticated()


def _is_sensitive_config_key(key: str) -> bool:
    upper = key.upper()
    sensitive_tokens = ("PASSWORD", "SECRET", "MASTER_KEY", "TOKEN", "PRIVATE_KEY", "API_KEY")
    return any(token in upper for token in sensitive_tokens)


def _public_config_snapshot() -> dict:
    raw = settings.model_dump()
    sanitized = {
        key: ("***" if value is not None and _is_sensitive_config_key(key) else value)
        for key, value in raw.items()
    }
    sanitized["DATABASE_URL_SAFE"] = settings.DATABASE_URL_SAFE
    sanitized["active_auth_providers"] = settings.active_auth_providers
    sanitized["desired_plugin_specs"] = settings.desired_plugin_specs
    sanitized["ldap_default_roles"] = settings.ldap_default_roles
    sanitized["oidc_admin_groups"] = settings.oidc_admin_groups
    return sanitized


class ConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]
    persist_in_vault: bool = True
    apply_runtime: bool = True


# ==========================================
# HTTP MIDDLEWARE (Interceptor)
# ==========================================
@app.middleware("http")
async def boot_interceptor(request: Request, call_next):
    allowed_prefixes = [
        "/_nicegui",
        "/static",
        "/assets",
        "/_pywebview",
        "/favicon.ico",
        "/site.webmanifest",
        "/setup",
        "/unseal",
    ]

    # Utilizing boot_service from the new path
    if getattr(boot_service, "is_booting", True):
        if request.url.path == "/" or any(
            request.url.path.startswith(p) for p in allowed_prefixes
        ):
            return await call_next(request)
        return RedirectResponse(url="/")

    return await call_next(request)


# ==========================================
# ROOT ROUTING (Entry Point)
# ==========================================
@app.get("/favicon.ico", include_in_schema=False)
async def favicon_redirect():
    return RedirectResponse(
        url=f"/assets/icons/favicon.ico?v={__version__}",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/site.webmanifest", include_in_schema=False)
async def manifest_redirect():
    return RedirectResponse(
        url=f"/assets/icons/site.webmanifest?v={__version__}",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@ui.page("/")
def entry_point():
    apply_theme(page_title="Home")

    if getattr(app.state, "maintenance", {}).get("active", False):
        attach_maintenance_overlay()
        return

    # 1. Check Vault Status
    if vault_instance.ui_state == "needs_init":
        ui.navigate.to("/setup")
        return

    if vault_instance.ui_state == "needs_unseal":
        ui.navigate.to("/unseal")
        return

    # 2. Boot Lock (Loading Screen)
    if boot_service.is_booting or vault_instance.ui_state == "loading":
        phase = getattr(boot_service, "phase", None)
        phase_label = phase.value.replace("_", " ") if phase else "initializing"
        with ui.column().classes("w-full h-screen items-center justify-center gap-4"):
            ui.spinner("dots", size="3em", color="white")
            ui.label("Lyndrix Boot Sequence...").classes(
                "text-zinc-500 text-sm tracking-widest uppercase"
            )
            ui.label(f"phase: {phase_label}").classes(
                "text-zinc-600 text-xs tracking-wide uppercase"
            )

        ui.timer(1.0, lambda: ui.navigate.to("/"), once=True)
        return

    # 3. System is ready
    # TODO: apply a centralized auth/authorization gate for protected pages like /dashboard, /settings, and /plugins.
    if _safe_is_authenticated():
        ui.navigate.to("/dashboard")
    else:
        ui.navigate.to("/login")


# ==========================================
# SYSTEM START & REGISTRATION
# ==========================================

register_auth_routes()
register_oidc_fastapi_routes(app)
register_settings_routes()
register_vault_routes()
register_dashboard_routes()

# Bind the running app to the plugin router registry so plugins that call
# ctx.register_routes() after startup get mounted immediately.
router_registry.mount_all(app)

# Permissions management API (groups, group permissions, per-user direct grants).
app.include_router(permissions_router)

# Socket management API (Docker, systemd, etc. with permission guards).
app.include_router(socket_router)


# ==========================================
# GLOBAL HEALTH CHECK
# ==========================================

@app.get("/api/health", tags=["System"])
async def global_health():
    """
    Aggregate health report for the Lyndrix core and all active plugins.

    Each plugin may implement ``async def health(ctx) -> PluginHealthStatus``
    in its ``entrypoint.py``.  Plugins without a ``health`` function are
    reported as ``"unknown"``.

    The top-level ``status`` field is the worst-case across all components:
    ``error`` > ``degraded`` > ``unknown`` > ``ok``.
    """
    import asyncio
    import time
    from core.components.plugins.logic.manager import module_manager

    plugin_results: dict = {}
    severity_order = {"error": 3, "degraded": 2, "unknown": 1, "ok": 0}
    worst = "ok"

    async def _call_health(module_id: str, entry: dict):
        module = entry.get("module")
        ctx = entry.get("context")
        if module is None or ctx is None or not hasattr(module, "health"):
            return module_id, PluginHealthStatus(status="unknown")
        try:
            t0 = time.monotonic()
            result = await asyncio.wait_for(module.health(ctx), timeout=5.0)
            elapsed_ms = (time.monotonic() - t0) * 1000
            if not isinstance(result, PluginHealthStatus):
                result = PluginHealthStatus(status="unknown", details={"raw": str(result)})
            result.latency_ms = round(elapsed_ms, 2)
            return module_id, result
        except asyncio.TimeoutError:
            return module_id, PluginHealthStatus(status="error", details={"reason": "health() timed out"})
        except Exception as exc:
            return module_id, PluginHealthStatus(status="error", details={"reason": str(exc)})

    tasks = [
        _call_health(mid, entry)
        for mid, entry in module_manager.registry.items()
        if entry.get("manifest") and entry["manifest"].type == "PLUGIN"
        and entry.get("status") == "active"
    ]

    gathered = await asyncio.gather(*tasks)
    for module_id, health_status in gathered:
        plugin_results[module_id] = health_status.model_dump()
        if severity_order.get(health_status.status, 0) > severity_order.get(worst, 0):
            worst = health_status.status

    return {
        "status": worst,
        "core_version": __core_version__,
        "api_version": __api_version__,
        "plugins": plugin_results,
    }


@app.get("/api/system/config", tags=["System"], summary="Get runtime config (sanitized)")
async def get_runtime_config(identity: ApiIdentity = Depends(require_permission("api:read"))):
    """Expose runtime settings from config.py with secrets redacted.

    Requires authentication and the ``api:read`` permission (system API key,
    HTTP Basic, per-user API key, or dashboard session).
    """
    return {
        "status": "ok",
        "authenticated_as": identity.username,
        "config": _public_config_snapshot(),
    }


@app.post("/api/system/config", tags=["System"], summary="Update runtime config")
async def set_runtime_config(
    payload: ConfigUpdateRequest,
    identity: ApiIdentity = Depends(require_permission("api:write")),
):
    """
    Update config keys via API.

    Requires authentication and the ``api:write`` permission (system API key,
    HTTP Basic, per-user API key, or dashboard session).

    - `updates`: key/value map of config fields.
    - `persist_in_vault`: if true, writes into `lyndrix/core/settings`.
    - `apply_runtime`: if true, applies validated Settings fields in-memory.
    """
    updates = payload.updates or {}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    settings_fields = set(settings.model_fields.keys())
    allowed_extra_keys = {"github_token", "system_api_key"}
    unknown_keys = sorted(set(updates.keys()) - settings_fields - allowed_extra_keys)
    if unknown_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported config keys: {', '.join(unknown_keys)}",
        )

    settings_updates = {k: v for k, v in updates.items() if k in settings_fields}

    validated_settings = None
    if settings_updates:
        try:
            merged = settings.model_dump()
            merged.update(settings_updates)
            validated_settings = Settings.model_validate(merged)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

    if payload.persist_in_vault:
        if not vault_instance.is_connected:
            raise HTTPException(
                status_code=503,
                detail="Vault is not connected; cannot persist config updates",
            )
        try:
            existing = {}
            try:
                resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                    path="core/settings",
                    mount_point="lyndrix",
                )
                existing = resp["data"]["data"] or {}
            except Exception:
                existing = {}

            existing.update(updates)
            vault_instance.client.secrets.kv.v2.create_or_update_secret(
                path="core/settings",
                mount_point="lyndrix",
                secret=existing,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to persist settings: {exc}") from exc

    if payload.apply_runtime and validated_settings is not None:
        for key in settings_updates.keys():
            setattr(settings, key, getattr(validated_settings, key))

    return {
        "status": "ok",
        "updated_keys": sorted(updates.keys()),
        "persisted_in_vault": payload.persist_in_vault,
        "applied_runtime": payload.apply_runtime and bool(settings_updates),
        "config": _public_config_snapshot(),
    }


@app.get("/api/auth/whoami", tags=["Authentication"], summary="Inspect API auth status")
async def auth_whoami(identity: ApiIdentity = Depends(optional_api_auth)):
    """
    Public endpoint describing the current API authentication state.

    Returns whether the request is authenticated, by which method, and whether
    the system API key mechanism is enabled on this instance. Useful for clients
    to verify their credentials without hitting a protected endpoint.
    """
    return {
        "authenticated": identity is not None,
        "method": identity.method if identity else None,
        "username": identity.username if identity else None,
        "roles": identity.roles if identity else [],
        "permissions": {
            "api:read": identity.allows("api:read") if identity else False,
            "api:write": identity.allows("api:write") if identity else False,
        },
        "system_api_key_enabled": system_api_key_configured(),
    }


@app.on_event("startup")
async def startup_event():
    log.info("STARTUP: Lyndrix Core Engine is starting...")
    bus.emit("system:started", {})


async def _hydrate_settings_from_vault(payload=None):
    """Apply UI-saved settings stored in Vault once Vault is available (env wins)."""
    try:
        settings.hydrate_from_vault()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"CONFIG: Vault settings hydration failed: {exc}")


bus.subscribe("vault:opened")(_hydrate_settings_from_vault)


ui.run_with(app, storage_secret=settings.STORAGE_SECRET)
