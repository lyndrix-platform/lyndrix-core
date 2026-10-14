"""
Plugin Router Registry
======================
A singleton that manages FastAPI routers contributed by plugins.

Plugins register an ``APIRouter`` via ``ModuleContext.register_routes()``.
The registry mounts each router under ``/api/plugins/<plugin-id>/`` and
tears it down cleanly when a plugin is deactivated.

Authentication is enforced **at the registry level**: every mounted plugin route
carries ``require_api_auth`` by default, so a plugin route can never be served
anonymously — even if the plugin author forgets a guard. Plugins still add
``Depends(require_permission("api:write"))`` on individual routes when they need
finer authorization than "any authenticated caller".

A plugin that genuinely needs an unauthenticated route (e.g. a webhook that
verifies its own signature, like a Discord/GitLab inbound hook) opts in with
``register(router, module_id, public=True)`` / ``ctx.register_routes(router,
public=True)``. This mounts the router WITHOUT ``require_api_auth`` — the
handler itself is then solely responsible for authenticating the request.

Runtime gate: every mounted plugin route (public ones too) also carries a
dependency that answers ``503`` with ``Retry-After`` while the plugin reports a
*blocking* ``starting``/``maintenance`` status via ``ctx.set_status`` — so a
plugin in its warm-up window never has to defend its own endpoints and the
shell gets a clean, retryable signal instead of timeouts.

Usage (from a plugin entrypoint)::

    from fastapi import APIRouter
    from core.api import APIRouter  # same thing, re-exported for convenience

    router = APIRouter()

    @router.get("/status")
    def status():               # already requires authentication (registry-level)
        return {"ok": True}

    def setup(ctx):
        ctx.register_routes(router)
"""

from __future__ import annotations

import json
import threading
from typing import Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from core.logger import get_logger

log = get_logger("Core:RouterRegistry")

RUNTIME_GATE_HEADER = "X-Plugin-Runtime"


def runtime_gate(module_id: str):
    """FastAPI dependency: 503 while *module_id* reports a blocking runtime phase.

    The body keeps ``detail`` a plain string (every plugin UI maps that to its
    error banner); the full runtime status travels in the ``X-Plugin-Runtime``
    header as JSON and in ``Retry-After``.
    """

    async def _gate() -> None:
        # Lazy import: the registry is imported by the manager at boot.
        from core.components.plugins.logic.manager import module_manager

        runtime = module_manager.get_runtime(module_id)
        if not runtime or not runtime.get("blocking"):
            return
        phase = runtime.get("phase")
        if phase not in ("starting", "maintenance"):
            return
        name = module_manager.display_name(module_id)
        message = runtime.get("message")
        retry_after = int(runtime.get("retry_after") or 10)
        raise HTTPException(
            status_code=503,
            detail=f"{name} is {phase}" + (f": {message}" if message else "") + " — retry shortly.",
            headers={
                "Retry-After": str(retry_after),
                RUNTIME_GATE_HEADER: json.dumps({"plugin_id": module_id, **runtime}),
            },
        )

    _gate.__qualname__ = f"runtime_gate<{module_id}>"
    return _gate


def _remote_router(module_id: str, role: str) -> APIRouter:
    """Catch-all router for a plugin pinned to a role this instance lacks.

    Every path answers 503 with ``X-Plugin-Runtime: {"phase": "remote", …}`` so
    clients (and the React shell) can say "runs on the <role> instance" instead
    of "not found". The plugin's real routes live on the pinned instance.
    """
    router = APIRouter()
    detail = f"this plugin runs on an instance with role '{role}' — not available on this instance"
    header = json.dumps({"plugin_id": module_id, "phase": "remote", "role": role, "blocking": True})

    async def _remote(path: str = "") -> None:
        raise HTTPException(status_code=503, detail=detail, headers={"Retry-After": "30", RUNTIME_GATE_HEADER: header})

    _remote.__qualname__ = f"remote_stub<{module_id}>"
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    router.add_api_route("", _remote, methods=methods, include_in_schema=False)
    router.add_api_route("/{path:path}", _remote, methods=methods, include_in_schema=False)
    return router


class PluginRouterRegistry:
    """Thread-safe registry for plugin-contributed FastAPI routers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # module_id → APIRouter
        self._routers: Dict[str, APIRouter] = {}
        # module_id → whether it was registered as public (no require_api_auth)
        self._public: Dict[str, bool] = {}
        # module_id → role, for plugins pinned to a role this instance lacks
        self._remote: Dict[str, str] = {}
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

    def register(self, module_id: str, router: APIRouter, public: bool = False) -> None:
        """
        Register an ``APIRouter`` for *module_id*.

        If the FastAPI app is already available the router is mounted
        immediately; otherwise it is queued for mounting when
        ``bind_app()`` is called.

        ``public=True`` mounts the router WITHOUT the default
        ``require_api_auth`` dependency — only for routes whose handler
        performs its own authentication (e.g. a webhook signature check).
        """
        with self._lock:
            if module_id in self._routers:
                log.warning("RouterRegistry: '%s' already has a registered router — replacing.", module_id)
            self._routers[module_id] = router
            self._public[module_id] = public
            app = self._app

        if app is not None:
            self._mount_router(app, module_id, router, public=public)
        else:
            log.debug("RouterRegistry: queued router for '%s' (app not yet bound)", module_id)

    def register_remote(self, module_id: str, role: str) -> None:
        """Mount the 503 "remote" stub for a plugin pinned to another role."""
        self.unmount(module_id)
        with self._lock:
            self._remote[module_id] = role
        self.register(module_id, _remote_router(module_id, role), public=False)

    def remote_role(self, module_id: str) -> Optional[str]:
        return self._remote.get(module_id)

    def unmount(self, module_id: str) -> None:
        """
        Remove all routes contributed by *module_id* from the live app.
        Safe to call even when the plugin has no registered router.
        """
        with self._lock:
            router = self._routers.pop(module_id, None)
            self._public.pop(module_id, None)
            self._remote.pop(module_id, None)
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

    def _mount_router(self, app: FastAPI, module_id: str, router: APIRouter, public: bool = False) -> None:
        prefix = self._prefix_for(module_id)
        # Lazy import keeps the registry free of import-order coupling at boot.
        from core.api.security import require_api_auth

        try:
            # Authentication is enforced at the registry level by default: every
            # plugin route gets require_api_auth, so a route can never be
            # anonymous even if the plugin author forgets. Plugins add
            # Depends(require_permission(...)) on their own routes for finer
            # authorization (e.g. api:write on mutations). public=True opts a
            # whole router out of this — the handler must self-authenticate.
            # Auth first (an anonymous caller learns nothing about the plugin's
            # state), then the runtime gate (503 while blocking warm-up/maintenance).
            dependencies = [] if public else [Depends(require_api_auth)]
            dependencies.append(Depends(runtime_gate(module_id)))
            if public:
                log.warning(
                    "RouterRegistry: mounting PUBLIC routes for '%s' at '%s' — "
                    "handler must self-authenticate.", module_id, prefix,
                )
            app.include_router(
                router,
                prefix=prefix,
                tags=[module_id],
                dependencies=dependencies,
            )
            # include_router() appends routes after NiceGUI's root catch-all (path "").
            # Move the newly added routes to before that catch-all so they match first.
            from core.api.route_order import move_routes_before_catchall

            move_routes_before_catchall(app, prefix)
            log.info("RouterRegistry: mounted router for '%s' at '%s'", module_id, prefix)
        except Exception as exc:
            log.error("RouterRegistry: failed to mount router for '%s': %s", module_id, exc)

    def _mount_pending(self) -> None:
        """Mount all routers in the registry onto the bound app."""
        with self._lock:
            items = [(mid, r, self._public.get(mid, False)) for mid, r in self._routers.items()]
            app = self._app

        if app is None:
            return

        for module_id, router, public in items:
            self._mount_router(app, module_id, router, public=public)


# Module-level singleton — import this everywhere.
router_registry = PluginRouterRegistry()
