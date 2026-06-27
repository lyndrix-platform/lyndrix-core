import asyncio
import os
import platform as _platform
import sys
import time as _time
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from nicegui import ui
from pydantic import BaseModel

from config import settings, EDITABLE_SETTINGS
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
from core.api.auth_api import auth_router
from core.api.events_api import events_router
from core.components.auth.api.users_api import users_router
from core.components.plugins.api.plugins_api import plugins_router
from core.components.settings.api.themes_api import themes_router
from core.components.vault.api.vault_api import vault_api_router
from core.components.sockets.api.socket_api import socket_router
from core.components.notification_router.api import notification_router_api

# --- Route Registrations ---
from core.components.auth.ui.routes import (
    register_auth_routes,
    register_oidc_fastapi_routes,
)
from core.components.settings.ui.routes import register_settings_routes
from core.components.vault.ui.routes import register_vault_routes
from core.components.dashboard.ui.routes import register_dashboard_routes
from core.components.notifications.api import register_notification_fastapi_routes, notifications_router

# --- Global UI ---
from ui.theme import apply_theme
from ui.maintenance import attach_maintenance_overlay
from version import __version__

setup_logging()

_startup_time = _time.monotonic()

app = FastAPI(
    title="Lyndrix Core API",
    description="Core and plugin endpoints for Lyndrix.",
    version=__version__,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    sanitized["env_locked"] = [
        s.field for s in EDITABLE_SETTINGS if os.getenv(s.field) is not None
    ]
    sanitized["editable_settings"] = [
        {
            "field": s.field,
            "label": s.label,
            "kind": s.kind,
            "options": s.options,
            "category": s.category,
        }
        for s in EDITABLE_SETTINGS
    ]
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
        "/app",  # React SPA (served from core) loads + shows its own boot/loading state
        "/_pywebview",
        "/favicon.ico",
        "/site.webmanifest",
        "/setup",
        "/unseal",
        "/api",
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
    # In 'react' mode the React SPA is the front door — send / to it. NiceGUI pages
    # (and this one in 'nicegui'/'both' mode) keep working underneath.
    if settings.LYNDRIX_UI_ENGINE == "react":
        ui.navigate.to("/app/")
        return

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

        # Poll and navigate when boot completes or vault state changes
        async def poll_boot_complete():
            while (boot_service.is_booting or vault_instance.ui_state == "loading"):
                await asyncio.sleep(0.2)
            ui.navigate.to("/")
        
        bus.create_tracked_task(poll_boot_complete(), name="boot_poll")
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
register_notification_fastapi_routes(app)

# Bind the running app to the plugin router registry so plugins that call
# ctx.register_routes() after startup get mounted immediately.
router_registry.mount_all(app)

# Permissions management API (groups, group permissions, per-user direct grants).
app.include_router(permissions_router)

# Socket management API (Docker, systemd, etc. with permission guards).
app.include_router(socket_router)

# Notification routing API (endpoint discovery, bindings, env-lock surface).
app.include_router(notification_router_api)

# Authenticated notification feed for the React frontend (notification bell).
app.include_router(notifications_router)

# Auth REST API — login / logout / me (used by lyndrix-ui).
app.include_router(auth_router)

# Server-Sent Events stream for real-time updates.
app.include_router(events_router)

# User and API key management.
app.include_router(users_router)

# Plugin lifecycle and marketplace management.
app.include_router(plugins_router)

# Theme management.
app.include_router(themes_router)

# Vault setup endpoints (unauthenticated — needed before Vault is ready).
app.include_router(vault_api_router)


# ==========================================
# GLOBAL HEALTH CHECK
# ==========================================

@app.get("/api/health", tags=["System"])
async def global_health(identity: Optional[ApiIdentity] = Depends(optional_api_auth)):
    """
    Aggregate health report for the Lyndrix core and all active plugins.

    Each plugin may implement ``async def health(ctx) -> PluginHealthStatus``
    in its ``entrypoint.py``.  Plugins without a ``health`` function are
    reported as ``"unknown"``.

    The top-level ``status`` field is the worst-case across all components:
    ``error`` > ``degraded`` > ``unknown`` > ``ok``.

    Anonymous callers receive only the coarse top-level ``status``; the detailed
    per-plugin breakdown, raw failure reasons and version numbers are returned
    only to authenticated callers with the ``api:read`` permission.
    """
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
            t0 = _time.monotonic()
            result = await asyncio.wait_for(module.health(ctx), timeout=5.0)
            elapsed_ms = (_time.monotonic() - t0) * 1000
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

    # Public callers only see the coarse status — no plugin inventory, failure
    # reasons or version numbers (avoid information disclosure / reconnaissance).
    if identity is None or not identity.allows("api:read"):
        return {"status": worst}

    return {
        "status": worst,
        "core_version": __core_version__,
        "api_version": __api_version__,
        "plugins": plugin_results,
    }


@app.get("/api/system/info", tags=["System"], summary="Runtime platform info")
async def system_info(identity: ApiIdentity = Depends(require_permission("api:read"))):
    """Read-only snapshot of runtime platform facts (versions, uptime, connectivity)."""
    from core.services import db_instance as _db
    db_ok = getattr(_db, "is_connected", None)
    return {
        "app_version": __version__,
        "core_version": __core_version__,
        "api_version": __api_version__,
        "python_version": sys.version.split()[0],
        "platform": _platform.system(),
        "uptime_s": round(_time.monotonic() - _startup_time),
        "vault_connected": vault_instance.is_connected,
        "db_connected": bool(db_ok) if db_ok is not None else None,
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

    # Reject env-locked keys up-front: an OS env var always wins on the next boot,
    # so persisting it to Vault would silently no-op and confuse the operator.
    locked = [key for key in updates if os.getenv(key) is not None]
    if locked:
        raise HTTPException(
            status_code=409,
            detail={"error": "env_locked", "locked_fields": sorted(locked)},
        )

    from core.components.settings.logic.system_config_service import system_config_service

    try:
        result = system_config_service.apply(
            updates,
            persist=payload.persist_in_vault,
            apply_runtime=payload.apply_runtime,
        )
    except ValueError as exc:
        # Structured pydantic errors or an unsupported-keys message.
        raise HTTPException(status_code=422, detail=exc.args[0]) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "status": "ok",
        "updated_keys": result["updated_keys"],
        "persisted_in_vault": result["persisted"],
        "applied_runtime": bool(result["applied_runtime"]),
        "config": _public_config_snapshot(),
    }


@app.post("/api/system/restart", tags=["System"], summary="Restart the core container")
async def system_restart(identity: ApiIdentity = Depends(require_permission("api:write"))):
    """Restart THIS core's own Docker container — e.g. to make a plugin upgrade take
    effect without an external ``docker restart`` (a recurring gotcha).

    Issues ``docker restart <self>`` over the mounted Docker socket. The 200 response is
    flushed first, then the restart fires ~1s later, so the client's connection drops as
    the container goes down and comes back — the UI should show "restarting" and poll for
    the core to return. Requires the ``api:write`` permission.
    """
    from core.components.sockets.providers.docker_provider import DockerProvider, own_container_id

    provider = DockerProvider()
    if not provider.is_available:
        raise HTTPException(status_code=503, detail="Docker socket/CLI unavailable — cannot self-restart.")
    cid = own_container_id()
    if not cid:
        raise HTTPException(status_code=503, detail="Could not determine own container id.")

    async def _do_restart():
        await asyncio.sleep(1)  # let the 200 flush before the container goes down
        ok, err = await provider.restart_container(cid)
        if not ok:
            log.error(f"SYSTEM: self-restart failed: {err}")

    asyncio.create_task(_do_restart())
    log.warning(f"SYSTEM: container self-restart requested by '{identity.username}' (container {cid[:12]}).")
    return {"status": "restarting", "container": cid[:12]}


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
    # TODO(agent): migrate to a FastAPI lifespan context manager once NiceGUI's
    # ui.run_with() startup hooks are confirmed compatible (a custom lifespan
    # suppresses on_event-registered handlers, which NiceGUI relies on).
    log.info("STARTUP: Lyndrix Core Engine is starting...")
    bus.emit("system:started", {})


async def _hydrate_settings_from_vault(payload=None):
    """Apply UI-saved settings stored in Vault once Vault is available (env wins)."""
    try:
        # hydrate_from_vault performs a blocking hvac round-trip — keep it off the loop.
        await asyncio.to_thread(settings.hydrate_from_vault)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"CONFIG: Vault settings hydration failed: {exc}")


bus.subscribe("vault:opened")(_hydrate_settings_from_vault)


ui.run_with(app, storage_secret=settings.STORAGE_SECRET)


# --- React SPA serving (selectable UI engine) ---
# Must run AFTER ui.run_with(): that call installs NiceGUI's root catch-all (path ""),
# and move_routes_before_catchall() splices the /app mount in front of it so the SPA is
# reachable. Served only when a built bundle exists at app/ui_dist (see the multi-stage
# Dockerfile / the dev build step). NiceGUI keeps owning / unless engine == "react".
if settings.LYNDRIX_UI_ENGINE in ("react", "both"):
    from pathlib import Path as _Path

    from starlette.exceptions import HTTPException as _StarletteHTTPException

    from core.api.route_order import move_routes_before_catchall

    class _SPAStaticFiles(StaticFiles):
        """StaticFiles that falls back to index.html on 404 so client-side routes
        (e.g. /app/dashboard, /app/apps/...) resolve on hard-load / refresh."""

        async def get_response(self, path, scope):
            try:
                return await super().get_response(path, scope)
            except _StarletteHTTPException as exc:
                if exc.status_code == 404:
                    return await super().get_response("index.html", scope)
                raise

    _ui_dist = _Path(__file__).parent / "ui_dist"
    if _ui_dist.is_dir() and (_ui_dist / "index.html").exists():
        app.mount("/app", _SPAStaticFiles(directory=str(_ui_dist), html=True), name="react_ui")
        move_routes_before_catchall(app, "/app")
        log.info("UI: React SPA served at /app (engine=%s)", settings.LYNDRIX_UI_ENGINE)
    else:
        log.warning(
            "UI: LYNDRIX_UI_ENGINE=%s but app/ui_dist is missing/empty — serving NiceGUI only.",
            settings.LYNDRIX_UI_ENGINE,
        )
