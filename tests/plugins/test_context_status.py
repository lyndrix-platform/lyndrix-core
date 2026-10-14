"""Tests for ``ModuleContext.set_status`` — plugin runtime status (startup
sequence / maintenance window) and the router runtime gate.

Run inside the lyndrix-core runtime:
    pytest tests/plugins/test_context_status.py -v
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from core.bus import bus
from core.components.plugins.logic.context import RUNTIME_STATUS_TOPIC, ModuleContext
from core.components.plugins.logic.manager import module_manager
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions
from core.api.router_registry import RUNTIME_GATE_HEADER, runtime_gate

PID = "lyndrix.plugin.test_runtime"


def _ctx(plugin_id: str = PID) -> ModuleContext:
    manifest = ModuleManifest(
        id=plugin_id, name="Runtime Tester", version="0.0.1", type="PLUGIN",
        permissions=ModulePermissions(emit=[], subscribe=[]),
    )
    return ModuleContext(manifest)


@pytest.fixture()
def events():
    """Collect plugin:runtime_status broadcasts for the test plugin."""
    seen: list[dict] = []

    def _listener(payload):
        if payload.get("plugin_id") == PID:
            seen.append(payload)

    _listener.__qualname__ = f"test_runtime_listener<{id(seen)}>"
    bus.subscribe(RUNTIME_STATUS_TOPIC)(_listener)
    try:
        yield seen
    finally:
        bus.unsubscribe(RUNTIME_STATUS_TOPIC, _listener)


@pytest.fixture()
def registered():
    """Register a context as an *active* module so get_runtime()/the gate see it."""
    ctx = _ctx()
    module_manager.registry[PID] = {"manifest": ctx.manifest, "module": None, "context": ctx, "status": "active"}
    try:
        yield ctx
    finally:
        module_manager.registry.pop(PID, None)


def test_default_runtime_is_ready():
    ctx = _ctx()
    assert ctx.runtime["phase"] == "ready" and ctx.runtime["blocking"] is False


def test_set_status_stores_and_broadcasts(events):
    ctx = _ctx()
    rt = ctx.set_status("starting", message="Initial probe sweep", progress=(0, 161), blocking=True)
    assert rt["phase"] == "starting" and rt["blocking"] is True
    assert rt["progress"] == {"done": 0, "total": 161} and rt["message"] == "Initial probe sweep"
    assert ctx.runtime == rt
    assert events[-1]["plugin_id"] == PID and events[-1]["phase"] == "starting"

    with pytest.raises(ValueError):
        ctx.set_status("exploded")


def test_blocking_only_applies_to_transient_phases(events):
    ctx = _ctx()
    assert ctx.set_status("degraded", blocking=True)["blocking"] is False
    assert ctx.set_status("ready", blocking=True)["blocking"] is False
    assert ctx.set_status("maintenance", blocking=True)["blocking"] is True


def test_progress_updates_are_coalesced_but_phase_changes_always_emit(events):
    ctx = _ctx()
    ctx.set_status("starting", progress=(0, 10), blocking=True)
    n = len(events)
    ctx.set_status("starting", progress=(1, 10), blocking=True)  # < 1 s later → coalesced
    ctx.set_status("starting", progress=(2, 10), blocking=True)
    assert len(events) == n and ctx.runtime["progress"]["done"] == 2  # state current, bus quiet
    ctx._runtime_emitted_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    ctx.set_status("starting", progress=(3, 10), blocking=True)  # interval elapsed → emitted
    assert len(events) == n + 1 and events[-1]["progress"]["done"] == 3
    ctx.set_status("ready")  # phase change → always emitted
    assert events[-1]["phase"] == "ready" and events[-1]["blocking"] is False


def test_since_is_kept_within_a_phase_and_reset_on_change():
    ctx = _ctx()
    first = ctx.set_status("starting", progress=(0, 2), blocking=True)["since"]
    assert ctx.set_status("starting", progress=(1, 2), blocking=True)["since"] == first
    assert ctx.set_status("ready")["since"] != first


def test_maintenance_context_manager_resets_even_on_error(events):
    ctx = _ctx()

    async def _run():
        async with ctx.maintenance("Re-import", retry_after=30):
            assert ctx.runtime["phase"] == "maintenance" and ctx.runtime["blocking"] is True
            assert ctx.runtime["retry_after"] == 30
        assert ctx.runtime["phase"] == "ready"
        with pytest.raises(RuntimeError):
            async with ctx.maintenance("boom"):
                raise RuntimeError("boom")
        assert ctx.runtime["phase"] == "ready"

    asyncio.run(_run())
    with ctx.maintenance("sync window", blocking=False):
        assert ctx.runtime["phase"] == "maintenance" and ctx.runtime["blocking"] is False
    assert ctx.runtime["phase"] == "ready"


def test_dispose_resets_runtime(events):
    ctx = _ctx()
    ctx.set_status("starting", blocking=True)
    ctx.dispose()
    assert ctx.runtime["phase"] == "ready" and events[-1]["phase"] == "ready"
    n = len(events)
    ctx.dispose()  # already ready → no extra broadcast
    assert len(events) == n


def test_manager_get_runtime_only_for_active_modules(registered):
    ctx = registered
    ctx.set_status("starting", blocking=True)
    assert module_manager.get_runtime(PID)["phase"] == "starting"
    module_manager.registry[PID]["status"] = "disabled"
    assert module_manager.get_runtime(PID) is None
    assert module_manager.get_runtime("lyndrix.plugin.nope") is None
    assert module_manager.display_name(PID) == "Runtime Tester"


def test_runtime_gate_503_only_while_blocking(registered):
    ctx = registered
    gate = runtime_gate(PID)

    asyncio.run(gate())  # ready → passes
    ctx.set_status("starting", message="warming up", blocking=False)
    asyncio.run(gate())  # non-blocking → passes
    ctx.set_status("degraded", blocking=True)
    asyncio.run(gate())  # degraded is never blocking

    ctx.set_status("starting", message="Initial probe sweep", progress=(3, 9), blocking=True, retry_after=15)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(gate())
    assert exc.value.status_code == 503
    assert "Runtime Tester is starting: Initial probe sweep" in exc.value.detail
    assert exc.value.headers["Retry-After"] == "15"
    assert '"phase": "starting"' in exc.value.headers[RUNTIME_GATE_HEADER]

    ctx.set_status("maintenance", blocking=True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(gate())
    assert exc.value.headers["Retry-After"] == "10"  # default

    ctx.set_status("ready")
    asyncio.run(gate())
    asyncio.run(runtime_gate("lyndrix.plugin.unknown")())  # unknown module → never gated
