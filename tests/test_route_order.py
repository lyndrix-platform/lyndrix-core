"""Tests for the NiceGUI catch-all route-ordering helper.

Plugin API routes are mounted after NiceGUI installs its root catch-all
(``path == ""``); ``move_routes_before_catchall`` must splice them ahead of it
so they are reachable. These tests pin that invariant.

Run inside the lyndrix-core runtime:
    pytest tests/test_route_order.py -v
"""
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from core.api.route_order import move_routes_before_catchall


def _catchall_index(app) -> int:
    return next(i for i, r in enumerate(app.router.routes) if getattr(r, "path", None) == "")


def _build_app_with_catchall_then_plugin() -> FastAPI:
    app = FastAPI()
    # Simulate NiceGUI's root catch-all: a route whose path is exactly "".
    app.router.add_api_route("", lambda: {"who": "catchall"}, methods=["GET"])
    # Plugin router mounted AFTER the catch-all (as happens at runtime).
    router = APIRouter()
    router.add_api_route("/ping", lambda: {"who": "plugin"}, methods=["GET"])
    app.include_router(router, prefix="/api/plugins/test.plugin")
    return app


def test_plugin_routes_moved_before_catchall():
    app = _build_app_with_catchall_then_plugin()
    prefix = "/api/plugins/test.plugin"

    # Before: the plugin route sits AFTER the catch-all.
    plugin_idx_before = next(
        i for i, r in enumerate(app.router.routes)
        if getattr(r, "path", "").startswith(prefix)
    )
    assert plugin_idx_before > _catchall_index(app)

    moved = move_routes_before_catchall(app, prefix)
    assert moved == 1

    # After: every plugin route is before the catch-all.
    catchall_idx = _catchall_index(app)
    plugin_indices = [
        i for i, r in enumerate(app.router.routes)
        if isinstance(r, APIRoute) and r.path.startswith(prefix)
    ]
    assert plugin_indices, "plugin route disappeared"
    assert all(i < catchall_idx for i in plugin_indices)

    # And it actually resolves (200), not shadowed.
    resp = TestClient(app).get("/api/plugins/test.plugin/ping")
    assert resp.status_code == 200
    assert resp.json() == {"who": "plugin"}


def test_no_catchall_is_safe_noop_append():
    """With no catch-all present the helper still works and reports the move."""
    app = FastAPI()
    router = APIRouter()
    router.add_api_route("/ping", lambda: {"ok": True}, methods=["GET"])
    app.include_router(router, prefix="/api/plugins/test.plugin")

    moved = move_routes_before_catchall(app, "/api/plugins/test.plugin")
    assert moved == 1
    assert TestClient(app).get("/api/plugins/test.plugin/ping").status_code == 200


def test_unknown_prefix_moves_nothing():
    app = _build_app_with_catchall_then_plugin()
    assert move_routes_before_catchall(app, "/api/plugins/does.not.exist") == 0
