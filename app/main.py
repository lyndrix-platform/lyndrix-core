from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from nicegui import ui

from config import settings
from core.bus import bus
from core.logger import setup_logging, get_logger
from core.session import is_authenticated

# --- FIX: Load exclusively from the facade ---
from core.services import vault_instance, boot_service

# --- Global API surface ---
from core.api import __api_version__, __core_version__, router_registry
from core.api import PluginHealthStatus

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
app = FastAPI(
    title="Lyndrix Core API",
    description="Core and plugin endpoints for Lyndrix.",
    version=__version__,
)
log = get_logger("Core:Main")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")


def _safe_is_authenticated() -> bool:
    return is_authenticated()


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


@app.on_event("startup")
async def startup_event():
    log.info("STARTUP: Lyndrix Core Engine is starting...")
    bus.emit("system:started", {})


ui.run_with(app, storage_secret=settings.STORAGE_SECRET)
