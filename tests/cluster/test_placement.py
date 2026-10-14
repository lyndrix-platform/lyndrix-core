"""Manifest ``placement`` + the manager's remote (503) handling for pinned plugins."""
import asyncio
import types

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from config import settings
from core.api.router_registry import RUNTIME_GATE_HEADER, router_registry
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import module_manager
from core.components.plugins.logic.models import ModuleManifest

PID = "lyndrix.plugin.test_placement"


def _manifest(placement: str) -> ModuleManifest:
    return ModuleManifest(id=PID, name="Placement Tester", version="0.0.1", type="PLUGIN", placement=placement)


def test_placement_validation():
    assert _manifest("any").placement == "any" and _manifest("any").pinned_role is None
    assert _manifest("SINGLETON").placement == "singleton"
    assert _manifest("pinned:runner").pinned_role == "runner"
    assert ModuleManifest(id=PID, name="x", version="1").placement == "any"
    for bad in ("pinned", "pinned:", "pinned:Runner Role", "worker", "pinned:a:b"):
        with pytest.raises(ValidationError):
            _manifest(bad)


@pytest.fixture()
def registered():
    calls = []
    module = types.SimpleNamespace(setup=lambda ctx: calls.append("setup"))

    def _register(placement: str):
        manifest = _manifest(placement)
        ctx = ModuleContext(manifest)
        module_manager.registry[PID] = {
            "manifest": manifest, "module": module, "context": ctx, "status": "initializing", "error_message": None,
        }
        return calls

    try:
        yield _register
    finally:
        module_manager.registry.pop(PID, None)
        router_registry.unmount(PID)


def test_pinned_plugin_without_the_role_becomes_remote(monkeypatch, registered):
    monkeypatch.setattr(settings, "LYNDRIX_INSTANCE_ROLES", "api,leader")
    calls = registered("pinned:runner")
    module_manager._execute_setup(PID)
    entry = module_manager.registry[PID]
    assert entry["status"] == "remote" and calls == []
    assert router_registry.remote_role(PID) == "runner"
    assert module_manager.get_runtime(PID) is None  # not active → no runtime status

    # The stub answers 503 + X-Plugin-Runtime phase=remote on every path.
    stub = router_registry._routers[PID]
    endpoint = stub.routes[-1].endpoint
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint("overview"))
    assert exc.value.status_code == 503
    assert '"phase": "remote"' in exc.value.headers[RUNTIME_GATE_HEADER]
    assert exc.value.headers["Retry-After"] == "30"

    # A second activation pass is a cheap no-op.
    module_manager._execute_setup(PID)
    assert calls == [] and entry["status"] == "remote"


def test_pinned_plugin_with_the_role_runs_setup(monkeypatch, registered):
    monkeypatch.setattr(settings, "LYNDRIX_INSTANCE_ROLES", "api,leader,runner")
    calls = registered("pinned:runner")
    module_manager._execute_setup(PID)
    assert module_manager.registry[PID]["status"] == "active" and calls == ["setup"]
    assert router_registry.remote_role(PID) is None


def test_disabling_a_remote_plugin_drops_the_stub(monkeypatch, registered):
    monkeypatch.setattr(settings, "LYNDRIX_INSTANCE_ROLES", "api")
    registered("pinned:runner")
    module_manager._execute_setup(PID)
    assert router_registry.remote_role(PID) == "runner"
    assert module_manager.toggle_module(PID, False, persist=False) is True
    assert module_manager.registry[PID]["status"] == "disabled"
    assert router_registry.remote_role(PID) is None
