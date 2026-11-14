"""CL-03: the "derived from a relayed event" mark must not leak into work that
outlives the event — plugins activated by a peer-triggered reconcile, lease
workers — or their cluster events are never published (p3)."""
import asyncio
import types

from core.bus import bus
from core.components.cluster.logic.bridge import ORIGIN_KEY, RELAY_ORIGIN, ClusterBridge
from core.components.cluster.logic.leases import RELEASED_TOPIC, LeaseKeeper
from core.components.cluster.logic.origin import clean_context
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import ModuleManifest


class RecordingCoordinator:
    driver, instance_id = "fake", "me"

    def __init__(self):
        self.published, self.callbacks, self.released = [], [], []

    async def publish(self, topic, payload):
        self.published.append((topic, payload.get("plugin_id") or payload.get("name")))

    def subscribe(self, callback):
        self.callbacks.append(callback)

    async def try_acquire(self, name, ttl=None):
        return True

    async def release(self, name):
        self.released.append(name)
        return True


def _plugin(mgr, mid, *, async_setup):
    """A plugin whose long-lived worker later announces a change to the peers
    and reports its runtime status — what monitoring/orchestrator do."""
    manifest = ModuleManifest(id=mid, name=mid, version="1.0.0", type="PLUGIN",
                              permissions={"emit": ["demo:changed"], "subscribe": []})
    module = types.ModuleType(f"plugins.{mid}.entrypoint")

    async def worker(ctx):
        await asyncio.sleep(0.05)
        ctx.emit("demo:changed", {"plugin_id": ctx.manifest.id}, cluster=True)
        ctx.set_status("degraded", message="x")          # plugin:runtime_status (core allowlist)

    if async_setup:
        async def setup(ctx):
            ctx.create_task(worker(ctx), name=f"{mid}:worker")
    else:
        def setup(ctx):
            asyncio.get_running_loop().create_task(worker(ctx))    # plain task: inherits setup's context

    module.setup = setup
    mgr.registry[mid] = {"manifest": manifest, "module": module, "context": ModuleContext(manifest),
                         "status": "disabled", "persisted": True}


def test_plugins_activated_by_a_peer_triggered_reconcile_can_publish(monkeypatch):
    coord = RecordingCoordinator()
    bridge = ClusterBridge(coord, "me")
    mgr = ModuleManager()
    _plugin(mgr, "demo_async", async_setup=True)
    _plugin(mgr, "demo_sync", async_setup=False)
    seen_origin = []

    async def fake_reconcile(*, trigger="manual"):
        # what reconcile_cluster_state does for a plugin a peer enabled
        seen_origin.append(RELAY_ORIGIN.get())
        mgr.toggle_module("demo_async", True, persist=False)
        mgr.toggle_module("demo_sync", True, persist=False)

    monkeypatch.setattr(mgr, "reconcile_cluster_state", fake_reconcile)

    async def run():
        bridge.attach()
        token = RELAY_ORIGIN.set("peer-1")               # as inside the bridge's relay dispatch
        try:
            await mgr._on_desired_changed({"plugin_id": "demo_async", "action": "enabled", ORIGIN_KEY: "peer-1"})
        finally:
            RELAY_ORIGIN.reset(token)
        await asyncio.sleep(0.3)
        bridge.detach()
        for mid in ("demo_async", "demo_sync"):
            mgr.registry[mid]["context"].dispose()

    asyncio.run(run())
    assert seen_origin == [None], "the reconcile itself still ran inside the peer's relay context"
    for mid in ("demo_async", "demo_sync"):
        assert ("demo:changed", mid) in coord.published, f"{mid}: cluster=True event was swallowed"
        assert ("plugin:runtime_status", mid) in coord.published, f"{mid}: runtime status was swallowed"


def test_direct_reactions_to_a_relayed_event_still_stay_local():
    """The 16-toast protection is untouched: only lifecycle boundaries are cut."""
    coord = RecordingCoordinator()
    bridge = ClusterBridge(coord, "me")

    async def _reacts(payload):
        bus.emit("ui:needs_refresh", {"reason": "derived"})

    _reacts.__qualname__ = f"relay_ctx_test_reacts<{id(coord)}>"
    bus.subscribe("plugin:installed")(_reacts)

    async def run():
        bridge.attach()
        try:
            coord.callbacks[0]("plugin:installed", {"repo": "x"}, "peer-1")
            await asyncio.sleep(0.05)
        finally:
            bridge.detach()
            bus.unsubscribe("plugin:installed", _reacts)

    asyncio.run(run())
    assert coord.published == []


def test_lease_callbacks_and_release_broadcasts_leave_the_relay_context():
    coord = RecordingCoordinator()
    bridge = ClusterBridge(coord, "me")
    keeper = LeaseKeeper(coord, ttl=30, heartbeat_s=10)
    origins = []

    async def start_worker():
        origins.append(("acquired", RELAY_ORIGIN.get()))
        bus.emit("demo:worker_started", {"plugin_id": "demo"}, cluster=True)

    def stop_worker():
        origins.append(("lost", RELAY_ORIGIN.get()))

    async def run():
        bridge.attach()
        token = RELAY_ORIGIN.set("peer-1")               # e.g. a relayed plugin:settings_changed handler
        try:
            keeper.want("demo:worker", acquired=start_worker, lost=stop_worker)
            await keeper.tick()
            await asyncio.sleep(0.02)
            keeper.forget("demo:worker", acquired=start_worker, lost=stop_worker, fire_lost=True)
            await asyncio.sleep(0.05)
        finally:
            RELAY_ORIGIN.reset(token)
            bridge.detach()

    asyncio.run(run())
    assert origins == [("acquired", None), ("lost", None)]
    assert ("demo:worker_started", "demo") in coord.published
    assert (RELEASED_TOPIC, "demo:worker") in coord.published, "peers were not told the lease is free"


def test_clean_context_only_drops_the_relay_mark():
    token = RELAY_ORIGIN.set("peer-9")
    try:
        assert clean_context().run(RELAY_ORIGIN.get) is None
        assert RELAY_ORIGIN.get() == "peer-9"            # the caller's context is untouched
    finally:
        RELAY_ORIGIN.reset(token)
