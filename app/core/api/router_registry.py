"""
Plugin Router Registry
======================
A singleton that manages FastAPI routers contributed by plugins.

Plugins register an ``APIRouter`` via ``ModuleContext.register_routes()``.
The registry mounts each router under ``/api/plugins/<plugin-id>/`` and
tears it down cleanly when a plugin is deactivated.

Usage (from a plugin entrypoint)::

    from fastapi import APIRouter
    from core.api import APIRouter  # same thing, re-exported for convenience

    router = APIRouter()

    @router.get("/status")
    def status():
        return {"ok": True}

    def setup(ctx):
        ctx.register_routes(router)
"""

from __future__ import annotations

import threading
from typing import Dict, Optional

from fastapi import APIRouter, FastAPI

from core.logger import get_logger

log = get_logger("Core:RouterRegistry")


class PluginRouterRegistry:
    """Thread-safe registry for plugin-contributed FastAPI routers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # module_id → APIRouter
        self._routers: Dict[str, APIRouter] = {}
        # Keep a reference to the FastAPI app once it's available.
        self._app: Optional[FastAPI] = None

    # ------------------------------------------------------------------
    # Lifecycle helpers called by the framework
    # ------------------------------------------------------------------

    def bind_app(self, app: FastAPI) -> None:
        """
        Store a reference to the running FastAPI app and immediately mount
        any routers that were registered before the app was available.
        """
        with self._lock:
            self._app = app

        if self._routers:
            log.debug("RouterRegistry: mounting %d pre-registered router(s)", len(self._routers))
            self._mount_pending()

    # ------------------------------------------------------------------
    # Plugin-facing API
    # ------------------------------------------------------------------

    def register(self, module_id: str, router: APIRouter) -> None:
        """
        Register an ``APIRouter`` for *module_id*.

        If the FastAPI app is already available the router is mounted
        immediately; otherwise it is queued for mounting when
        ``bind_app()`` is called.
        """
        with self._lock:
            if module_id in self._routers:
                log.warning("RouterRegistry: '%s' already has a registered router — replacing.", module_id)
            self._routers[module_id] = router
            app = self._app

        if app is not None:
            self._mount_router(app, module_id, router)
        else:
            log.debug("RouterRegistry: queued router for '%s' (app not yet bound)", module_id)

    def unmount(self, module_id: str) -> None:
        """
        Remove all routes contributed by *module_id* from the live app.
        Safe to call even when the plugin has no registered router.
        """
        with self._lock:
            router = self._routers.pop(module_id, None)
            app = self._app

        if router is None or app is None:
            return

        prefix = self._prefix_for(module_id)
        try:
            before = len(app.router.routes)
            app.router.routes[:] = [
                route for route in app.router.routes
                if not getattr(route, "path", "").startswith(prefix)
            ]
            removed = before - len(app.router.routes)
            app.openapi_schema = None
            log.info("RouterRegistry: removed %d route(s) for '%s'", removed, module_id)
        except Exception as exc:
            log.error("RouterRegistry: failed to remove routes for '%s': %s", module_id, exc)

    def mount_all(self, app: FastAPI) -> None:
        """
        Mount every registered router onto *app*.
        Idempotent — already-mounted routers are skipped.
        Called once from ``main.py`` at startup.
        """
        self.bind_app(app)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _prefix_for(module_id: str) -> str:
        # Convert e.g. "lyndrix.plugin.discord_notifier" → "/api/plugins/lyndrix.plugin.discord_notifier"
        return f"/api/plugins/{module_id}"

    def _mount_router(self, app: FastAPI, module_id: str, router: APIRouter) -> None:
        prefix = self._prefix_for(module_id)
        try:
            app.include_router(router, prefix=prefix, tags=[module_id])
            app.openapi_schema = None
            log.info("RouterRegistry: mounted router for '%s' at '%s'", module_id, prefix)
        except Exception as exc:
            log.error("RouterRegistry: failed to mount router for '%s': %s", module_id, exc)

    def _mount_pending(self) -> None:
        """Mount all routers in the registry onto the bound app."""
        with self._lock:
            items = list(self._routers.items())
            app = self._app

        if app is None:
            return

        for module_id, router in items:
            self._mount_router(app, module_id, router)


# Module-level singleton — import this everywhere.
router_registry = PluginRouterRegistry()
