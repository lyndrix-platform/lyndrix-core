"""BG-01: disabling a plugin must take its REST API down.

``toggle_module(active=False)`` used to unmount routes only for ``remote``
plugins, so ``/api/plugins/<id>/*`` kept answering 200 against a service whose
tasks and subscriptions were already disposed. The runtime gate is the second
line of defence for routers that survive in a non-running plugin.
"""
import types

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import core.services  # noqa: F401  (import order: services before api)
from core.api.router_registry import RUNTIME_GATE_HEADER, router_registry
from core.components.plugins.logic import manager as manager_mod
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import module_manager
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

PID = "lyndrix.plugin.test_toggle_routes"
PREFIX = f"/api/plugins/{PID}"


def _router() -> APIRouter:
    r = APIRouter()

    @r.get("/ping")
    def ping():
        return {"ok": True}

    return r


@pytest.fixture()
def client(monkeypatch):
    """A bound FastAPI app; persistence and the ``main`` import are stubbed out."""
    app = FastAPI()
    monkeypatch.setattr(router_registry, "_app", app)
    monkeypatch.setattr(manager_mod.db_instance, "SessionLocal", None, raising=False)
    monkeypatch.setattr(module_manager, "_ensure_plugin_state_schema", lambda: None)
    monkeypatch.setattr(module_manager, "_teardown_ui", lambda mid: None)
    monkeypatch.setattr(module_manager, "_mount_plugin_statics", lambda mid: None)
    try:
        yield TestClient(app)
    finally:
        router_registry.unmount(PID)
        module_manager.registry.pop(PID, None)


def _register(setup, **manifest_kw) -> ModuleContext:
    manifest = ModuleManifest(
        id=PID, name="Toggle Tester", version="0.0.1", type="PLUGIN",
        permissions=ModulePermissions(emit=[], subscribe=[]), **manifest_kw,
    )
    ctx = ModuleContext(manifest)
    module = types.SimpleNamespace(
        setup=setup, __name__="plugins.test_toggle_routes.entrypoint",
        __file__="/nonexistent/plugins/test_toggle_routes/entrypoint.py",
    )
    module_manager.registry[PID] = {
        "manifest": manifest, "module": module, "context": ctx,
        "status": "initializing", "error_message": None,
    }
    return ctx


def test_disable_unmounts_routes_and_enable_mounts_them_again(client):
    _register(lambda ctx: ctx.register_routes(_router(), public=True))

    assert module_manager.toggle_module(PID, True, persist=False) is True
    assert client.get(f"{PREFIX}/ping").json() == {"ok": True}

    assert module_manager.toggle_module(PID, False, persist=False) is True
    assert client.get(f"{PREFIX}/ping").status_code == 404
    assert not [r for r in client.app.router.routes if getattr(r, "path", "").startswith(PREFIX)]

    assert module_manager.toggle_module(PID, True, persist=False) is True
    assert client.get(f"{PREFIX}/ping").json() == {"ok": True}


def test_gate_answers_503_for_a_router_left_behind_by_a_failed_setup(client):
    def setup(ctx):
        ctx.register_routes(_router(), public=True)
        raise RuntimeError("boom after register_routes")

    _register(setup)
    module_manager.toggle_module(PID, True, persist=False)
    assert module_manager.registry[PID]["status"] == "failed"

    resp = client.get(f"{PREFIX}/ping")
    assert resp.status_code == 503 and "not active" in resp.json()["detail"]


def test_blocking_runtime_status_still_answers_503_with_retry_after(client):
    ctx = _register(lambda c: c.register_routes(_router(), public=True))
    module_manager.toggle_module(PID, True, persist=False)

    ctx.set_status("maintenance", message="Re-import", blocking=True, retry_after=7)
    resp = client.get(f"{PREFIX}/ping")
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "7"
    assert '"phase": "maintenance"' in resp.headers[RUNTIME_GATE_HEADER]

    ctx.set_status("ready")
    assert client.get(f"{PREFIX}/ping").status_code == 200


def test_remote_plugin_keeps_its_503_stub_until_disabled(client, monkeypatch):
    """Pinned to a role this instance lacks: 503 phase=remote, not the inactive gate."""
    monkeypatch.setattr(manager_mod.settings, "LYNDRIX_INSTANCE_ROLES", "api", raising=False)
    _register(lambda ctx: pytest.fail("setup() must not run on a non-pinned instance"),
              placement="pinned:runner")

    module_manager.toggle_module(PID, True, persist=False)
    assert module_manager.registry[PID]["status"] == "remote"
    # Authenticated route: the stub is mounted non-public, so bypass auth here.
    from core.api.security import require_api_auth
    client.app.dependency_overrides[require_api_auth] = lambda: None

    resp = client.get(f"{PREFIX}/anything")
    assert resp.status_code == 503
    assert '"phase": "remote"' in resp.headers[RUNTIME_GATE_HEADER]

    module_manager.toggle_module(PID, False, persist=False)
    assert client.get(f"{PREFIX}/anything").status_code == 404
