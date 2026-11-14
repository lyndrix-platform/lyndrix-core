"""A plugin reload registers a fresh router: the previous routes must leave the
live app, otherwise Starlette keeps answering with the stale handlers (bound to
the unloaded module and its disposed ORM classes → 500s after the 2nd reload)."""
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.api.router_registry import PluginRouterRegistry

PID = "lyndrix.plugin.test_router_replace"


def _router(answer: str) -> APIRouter:
    r = APIRouter()

    @r.get("/ping")
    def ping():
        return {"answer": answer}

    return r


def _prefixed(app: FastAPI, prefix: str) -> list[str]:
    return [getattr(r, "path", "") for r in app.router.routes if getattr(r, "path", "").startswith(prefix)]


def test_reregister_replaces_the_previous_routes():
    app = FastAPI()
    reg = PluginRouterRegistry()
    reg.bind_app(app)
    prefix = reg._prefix_for(PID)

    reg.register(PID, _router("first"), public=True)
    reg.register(PID, _router("second"), public=True)
    reg.register(PID, _router("third"), public=True)

    assert _prefixed(app, prefix) == [f"{prefix}/ping"]          # exactly one mount left
    client = TestClient(app)
    assert client.get(f"{prefix}/ping").json() == {"answer": "third"}

    reg.unmount(PID)
    assert _prefixed(app, prefix) == []
    assert client.get(f"{prefix}/ping").status_code == 404


# --- BG-16: several routers per plugin, "/" boundary -----------------------------

def _single_route_router(path: str, answer: str) -> APIRouter:
    r = APIRouter()

    @r.get(path)
    def handler():
        return {"answer": answer}

    return r


def test_authenticated_and_public_router_of_one_plugin_coexist():
    """The split register_routes() invites: an authenticated API router plus a
    public webhook router. The second call used to replace the first."""
    from core.api.security import require_api_auth

    app = FastAPI()
    app.dependency_overrides[require_api_auth] = lambda: None
    reg = PluginRouterRegistry()
    reg.bind_app(app)
    prefix = reg._prefix_for(PID)

    reg.register(PID, _single_route_router("/status", "api"))
    reg.register(PID, _single_route_router("/webhook", "hook"), public=True)

    client = TestClient(app)
    assert client.get(f"{prefix}/status").json() == {"answer": "api"}
    assert client.get(f"{prefix}/webhook").json() == {"answer": "hook"}

    reg.unmount(PID)
    assert _prefixed(app, prefix) == []


def test_same_router_object_registered_again_is_mounted_once():
    """setup() retried after a failure registers its module-level router again."""
    app = FastAPI()
    reg = PluginRouterRegistry()
    reg.bind_app(app)
    prefix = reg._prefix_for(PID)
    api, hook = _single_route_router("/status", "api"), _single_route_router("/webhook", "hook")

    for _ in range(2):
        reg.register(PID, api, public=True)
        reg.register(PID, hook, public=True)

    assert sorted(_prefixed(app, prefix)) == [f"{prefix}/status", f"{prefix}/webhook"]


def test_routers_queued_before_the_app_is_bound_are_all_mounted():
    reg = PluginRouterRegistry()
    reg.register(PID, _single_route_router("/status", "api"), public=True)
    reg.register(PID, _single_route_router("/webhook", "hook"), public=True)
    app = FastAPI()
    reg.bind_app(app)
    prefix = reg._prefix_for(PID)
    assert sorted(_prefixed(app, prefix)) == [f"{prefix}/status", f"{prefix}/webhook"]


def test_unmount_leaves_a_sibling_with_a_longer_id_alone():
    """`lyndrix.plugin.ai` vs `lyndrix.plugin.ai_assistant`: a bare startswith()
    removed the sibling's routes too."""
    app = FastAPI()
    reg = PluginRouterRegistry()
    reg.bind_app(app)
    short, long_ = "lyndrix.plugin.ai", "lyndrix.plugin.ai_assistant"

    reg.register(short, _single_route_router("/x", "short"), public=True)
    reg.register(long_, _single_route_router("/y", "long"), public=True)
    reg.unmount(short)

    client = TestClient(app)
    assert client.get(f"/api/plugins/{short}/x").status_code == 404
    assert client.get(f"/api/plugins/{long_}/y").json() == {"answer": "long"}
