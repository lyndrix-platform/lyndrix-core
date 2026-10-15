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
