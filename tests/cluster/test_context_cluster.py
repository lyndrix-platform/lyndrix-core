"""ModuleContext cluster API (core API 1.7) against the real cluster service in
single mode (local driver): one code path for single and cluster."""
import asyncio

import pytest

from core.bus import bus
from core.components.cluster.logic.cluster_service import cluster_service
from core.components.plugins.logic.context import RUNTIME_STATUS_TOPIC, ModuleContext
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

PID = "lyndrix.plugin.test_cluster_ctx"


def _ctx(placement: str = "any", emit=()) -> ModuleContext:
    manifest = ModuleManifest(
        id=PID, name="Cluster Tester", version="0.0.1", type="PLUGIN",
        permissions=ModulePermissions(emit=list(emit), subscribe=[]), placement=placement,
    )
    return ModuleContext(manifest)


def test_single_mode_identity_accessors():
    ctx = _ctx()
    assert cluster_service.mode == "single" and ctx.cluster_mode == "single"
    assert ctx.instance_id == cluster_service.instance_id
    assert ctx.cluster_size == 1
    assert "api" in ctx.roles


def test_on_lease_is_granted_in_single_mode_and_dispose_forgets():
    ctx = _ctx()
    events = []
    ctx.on_lease("test:prober", acquired=lambda: events.append("acq"), lost=lambda: events.append("lost"))
    try:
        asyncio.run(cluster_service.leases.tick())
        assert events == ["acq"] and ctx.holds_lease("test:prober")
        assert "test:prober" in cluster_service.leases.held
    finally:
        ctx.dispose()
    asyncio.run(asyncio.sleep(0.02))
    assert not cluster_service.leases.holds("test:prober")
    assert "test:prober" not in cluster_service.leases.wanted
    assert events == ["acq"]  # teardown does not fire lost


def test_singleton_placement_binds_every_lease_to_the_plugin_lease():
    ctx = _ctx(placement="singleton")
    got = []
    ctx.on_lease("a", acquired=lambda: got.append("a"))
    ctx.on_lease("b", acquired=lambda: got.append("b"))
    try:
        single = f"plugin:{PID}:singleton"
        assert cluster_service.leases.wanted.count(single) == 1
        assert "a" not in cluster_service.leases.wanted
        asyncio.run(cluster_service.leases.tick())
        assert sorted(got) == ["a", "b"] and ctx.holds_lease("whatever")
    finally:
        ctx.dispose()


def test_one_shot_lease_scope_acquires_and_releases():
    ctx = _ctx()

    async def run():
        async with ctx.lease("test:oneshot") as held:
            assert held is True
            info = await cluster_service.coordinator.holder("test:oneshot")
            assert info is not None and info.holder == cluster_service.instance_id
        assert await cluster_service.coordinator.holder("test:oneshot") is None

    asyncio.run(run())


def test_lease_scope_reuses_a_lease_held_via_on_lease():
    ctx = _ctx()
    ctx.on_lease("test:held", acquired=lambda: None)
    try:
        async def run():
            await cluster_service.leases.tick()
            async with ctx.lease("test:held") as held:
                assert held is True
            # still held by the keeper — the scope did not release it
            assert cluster_service.leases.holds("test:held")

        asyncio.run(run())
    finally:
        ctx.dispose()


def test_emit_cluster_flag_still_honours_permissions():
    denied = _ctx()
    with pytest.raises(PermissionError):
        denied.emit("test:topic", {"a": 1}, cluster=True)
    allowed = _ctx(emit=["test:topic"])
    allowed.emit("test:topic", {"a": 1}, cluster=True)  # no error; local driver → no-op publish


def test_runtime_status_carries_the_instance_id():
    seen = []

    def _listener(payload):
        if payload.get("plugin_id") == PID:
            seen.append(payload)

    _listener.__qualname__ = f"cluster_ctx_runtime_listener<{id(seen)}>"
    bus.subscribe(RUNTIME_STATUS_TOPIC)(_listener)
    try:
        _ctx().set_status("starting", blocking=True)
    finally:
        bus.unsubscribe(RUNTIME_STATUS_TOPIC, _listener)
    assert seen and seen[-1]["instance_id"] == cluster_service.instance_id


def test_cluster_instances_describe_self_in_single_mode():
    """core API 1.7.1: a role-split plugin asks who is alive with which roles."""
    ctx = _ctx()
    instances = ctx.cluster_instances
    assert len(instances) >= 1
    me = [i for i in instances if i["is_self"]]
    assert me and me[0]["instance_id"] == ctx.instance_id
    assert set(me[0]["roles"]) == set(ctx.roles) and me[0]["alive"] is True
    assert ctx.has_live_role("api") is True
    assert ctx.has_live_role("no-such-role") is False


def test_release_lease_fires_lost_and_frees_the_lease():
    """API 1.9: a plugin gives a wanted lease up at runtime (e.g. narrowed
    probe_roles) — its ``lost`` callback runs once and the lease is free."""
    ctx = _ctx()
    events = []
    ctx.on_lease("test:release", acquired=lambda: events.append("acq"), lost=lambda: events.append("lost"))
    try:
        asyncio.run(cluster_service.leases.tick())
        assert events == ["acq"] and ctx.holds_lease("test:release")
        assert ctx.release_lease("test:release") is True
        asyncio.run(asyncio.sleep(0.02))
        assert events == ["acq", "lost"]
        assert not cluster_service.leases.holds("test:release")
        assert "test:release" not in cluster_service.leases.wanted
        assert ctx.release_lease("test:release") is False   # nothing registered any more
        # Wanting it again works like the first time.
        ctx.on_lease("test:release", acquired=lambda: events.append("acq2"))
        asyncio.run(cluster_service.leases.tick())
        assert events == ["acq", "lost", "acq2"]
    finally:
        ctx.dispose()
