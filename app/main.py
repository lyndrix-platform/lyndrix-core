from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from config import settings
from core.bus import bus
from core.logger import setup_logging, get_logger
from core.session import is_authenticated

# --- FIX: Load exclusively from the facade ---
from core.services import vault_instance, boot_service

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

setup_logging()
app = FastAPI()
log = get_logger("Core:Main")


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
        "/_pywebview",
        "/favicon.ico",
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
@ui.page("/")
def entry_point():
    apply_theme()

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
        ui.query("body").style("background-color: #09090b;")
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


@app.on_event("startup")
async def startup_event():
    log.info("STARTUP: Lyndrix Core Engine is starting...")
    bus.emit("system:started", {})


ui.run_with(app, storage_secret=settings.STORAGE_SECRET)
