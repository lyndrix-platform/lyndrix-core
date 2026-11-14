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
The same gate answers ``503`` for a plugin whose registry status is
``disabled``/``failed``/``degraded``/``blocked`` (its routes are unmounted on
disable; the gate is the second line of defence).

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
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from core.api.route_order import move_routes_before_catchall, path_under_prefix
from core.logger import get_logger

log = get_logger("Core:RouterRegistry")

RUNTIME_GATE_HEADER = "X-Plugin-Runtime"
# Registry statuses in which a plugin's own handlers must never run.
INACTIVE_STATUSES = ("disabled", "failed", "degraded", "blocked")


def runtime_gate(module_id: str):
    """FastAPI dependency: 503 while *module_id* reports a blocking runtime phase.

    The body keeps ``detail`` a plain string (every plugin UI maps that to its
    error banner); the full runtime status travels in the ``X-Plugin-Runtime``
    header as JSON and in ``Retry-After``.
    """

    async def _gate() -> None:
        # Lazy import: the registry is imported by the manager at boot.
        from core.components.plugins.logic.manager import module_manager

        # Defence in depth: a plugin that is not running must not serve its API,
        # even if a router survived (setup() failed half-way, or a future code
        # path forgets to unmount). Disabling unmounts the routes; this gate
        # catches whatever is still mounted. "remote" passes — its stub router
        # answers 503 phase=remote itself.
        entry = module_manager.registry.get(module_id)
        status = entry.get("status") if entry else None
        if status in INACTIVE_STATUSES:
            name = module_manager.display_name(module_id)
            # No X-Plugin-Runtime header: that one announces a transient phase
            # clients retry on; an inactive plugin is not coming back by itself.
            raise HTTPException(
                status_code=503,
                detail=f"{name} is not active on this instance (status: {status}).",
            )

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


def _remote_role_state(role: str) -> tuple[str, int, str]:
    """(role state, Retry-After seconds, detail) for the 503 of a pinned plugin.

    ``alive``: the request reached the wrong instance — a load-balancer route
    is missing, retrying soon will not help (30 s as before). ``draining`` /
    ``restarting``: the role is on its way back (planned restart) — retry in a
    few seconds. ``dead``: nobody carries the role.
    """
    state = "unknown"
    try:
        from core.components.cluster.logic.cluster_service import cluster_service

        state = cluster_service.role_state(role)["state"]
    except Exception:  # pragma: no cover - never let diagnostics break the stub
        log.debug("RouterRegistry: role state lookup failed", exc_info=True)
    where = f"an instance with role '{role}'"
    if state in ("draining", "restarting"):
        return state, 5, f"this plugin runs on {where}, which is restarting — retry shortly"
    if state == "dead":
        return state, 30, f"this plugin runs on {where} — no such instance is online"
    if state == "unknown":
        return state, 10, f"this plugin runs on {where} — not available on this instance"
    return state, 30, f"this plugin runs on {where} — not available on this instance"


def _remote_router(module_id: str, role: str) -> APIRouter:
    """Catch-all router for a plugin pinned to a role this instance lacks.

    Every path answers 503 with ``X-Plugin-Runtime: {"phase": "remote", …}`` so
    clients (and the React shell) can say "runs on the <role> instance" instead
    of "not found". The plugin's real routes live on the pinned instance.
    """
    router = APIRouter()

    async def _remote(path: str = "") -> None:
        state, retry_after, detail = _remote_role_state(role)
        # Same keys as before plus role_state / retry_after / message — clients
        # that only look at "phase": "remote" keep working.
        header = json.dumps({
            "plugin_id": module_id, "phase": "remote", "role": role, "blocking": True,
            "role_state": state, "retry_after": retry_after, "message": detail,
        })
        raise HTTPException(status_code=503, detail=detail,
                            headers={"Retry-After": str(retry_after), RUNTIME_GATE_HEADER: header})

    _remote.__qualname__ = f"remote_stub<{module_id}>"
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    router.add_api_route("", _remote, methods=methods, include_in_schema=False)
    router.add_api_route("/{path:path}", _remote, methods=methods, include_in_schema=False)
    return router


class _Registration:
    """One router a plugin registered, plus the app routes mounted for it."""

    __slots__ = ("router", "public", "mounted")

    def __init__(self, router: APIRouter, public: bool) -> None:
        self.router = router
        self.public = public
        # The route objects include_router() created on the app for this
        # router — removed by identity when only this registration is replaced.
        self.mounted: list = []

    def signature(self) -> set:
        """``(path, method)`` pairs this router serves (``*`` = any method)."""
        pairs = set()
        for route in self.router.routes:
            path = getattr(route, "path", "")
            for method in (getattr(route, "methods", None) or {"*"}):
                pairs.add((path, method))
        return pairs


class PluginRouterRegistry:
    """Thread-safe registry for plugin-contributed FastAPI routers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # module_id → routers registered by that plugin, in registration order.
        # A plugin may hold several (e.g. an authenticated one plus a
        # ``public=True`` webhook router).
        self._registrations: Dict[str, List[_Registration]] = {}
        # module_id → role, for plugins pinned to a role this instance lacks
        self._remote: Dict[str, str] = {}
        # Keep a reference to the FastAPI app once it's available.
        self._app: Optional[FastAPI] = None

    @property
    def _routers(self) -> Dict[str, APIRouter]:
        """module_id → the most recently registered router (read-only view)."""
        with self._lock:
            return {mid: regs[-1].router for mid, regs in self._registrations.items() if regs}

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
            pending = any(self._registrations.values())

        if pending:
            log.debug("RouterRegistry: mounting pre-registered router(s)")
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

        A plugin may register several routers (typically an authenticated one
        and a public webhook router); they are kept side by side. A router
        *replaces* an earlier registration of the same plugin when it is the
        same object or serves one of the same ``(path, method)`` pairs — that
        is a re-registration (setup() retried, plugin reloaded), and the stale
        routes would otherwise shadow the new ones.
        """
        fresh = _Registration(router, public)
        fresh_signature = fresh.signature()
        with self._lock:
            current = self._registrations.setdefault(module_id, [])
            replaced = [
                reg for reg in current
                if reg.router is router or (reg.signature() & fresh_signature)
            ]
            for reg in replaced:
                current.remove(reg)
            current.append(fresh)
            app = self._app
        if replaced:
            # Starlette matches the first route, and include_router() appends —
            # the stale handlers (closures over the unloaded module, whose ORM
            # classes are disposed) would keep answering with 500s.
            log.warning("RouterRegistry: '%s' re-registered a router — replacing the previous one.", module_id)
            if app is not None:
                self._remove_routes(app, module_id, [r for reg in replaced for r in reg.mounted])

        if app is not None:
            self._mount_router(app, module_id, fresh)
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
            registrations = self._registrations.pop(module_id, None)
            self._remote.pop(module_id, None)
            app = self._app

        if not registrations or app is None:
            return

        # Everything at or below the plugin's prefix goes — with a "/" boundary,
        # so a sibling whose id merely starts with this one keeps its routes.
        prefix = self._prefix_for(module_id)
        self._remove_routes(
            app, module_id,
            [route for route in app.router.routes if path_under_prefix(getattr(route, "path", ""), prefix)],
        )

    @staticmethod
    def _remove_routes(app: FastAPI, module_id: str, doomed: list) -> None:
        """Drop the given route objects (by identity) from the live app."""
        try:
            doomed_ids = {id(route) for route in doomed}
            before = len(app.router.routes)
            app.router.routes[:] = [route for route in app.router.routes if id(route) not in doomed_ids]
            app.openapi_schema = None
            log.info("RouterRegistry: removed %d route(s) for '%s'", before - len(app.router.routes), module_id)
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

    def _mount_router(self, app: FastAPI, module_id: str, registration: _Registration) -> None:
        prefix = self._prefix_for(module_id)
        public = registration.public
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
            existing = {id(route) for route in app.router.routes}
            app.include_router(
                registration.router,
                prefix=prefix,
                tags=[module_id],
                dependencies=dependencies,
            )
            registration.mounted = [route for route in app.router.routes if id(route) not in existing]
            # include_router() appends routes after NiceGUI's root catch-all (path "").
            # Move the newly added routes to before that catch-all so they match first.
            move_routes_before_catchall(app, prefix)
            log.info("RouterRegistry: mounted router for '%s' at '%s'", module_id, prefix)
        except Exception as exc:
            log.error("RouterRegistry: failed to mount router for '%s': %s", module_id, exc)

    def _mount_pending(self) -> None:
        """Mount every registered router that is not on the bound app yet."""
        with self._lock:
            items = [
                (mid, reg) for mid, regs in self._registrations.items() for reg in regs if not reg.mounted
            ]
            app = self._app

        if app is None:
            return

        for module_id, registration in items:
            self._mount_router(app, module_id, registration)


# Module-level singleton — import this everywhere.
router_registry = PluginRouterRegistry()
