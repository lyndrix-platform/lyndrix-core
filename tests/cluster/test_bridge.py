"""Bus bridge: allowlist + cluster=True go out, _origin never goes out twice,
remote events are re-emitted locally with _origin."""
import asyncio

from core.bus import bus
from core.components.cluster.logic.bridge import CLUSTER_TOPICS, ORIGIN_KEY, ClusterBridge


class RecordingCoordinator:
    driver = "fake"
    instance_id = "me"

    def __init__(self):
        self.published = []
        self.callbacks = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    def subscribe(self, callback):
        self.callbacks.append(callback)


def test_bridge_publishes_allowlisted_and_forced_topics_only():
    coord = RecordingCoordinator()
    bridge = ClusterBridge(coord, "me")

    async def run():
        bridge.attach()
        try:
            assert "plugin:state_changed" in CLUSTER_TOPICS
            bus.emit("plugin:state_changed", {"plugin_id": "x"})          # allowlisted
            bus.emit("monitoring:monitors_changed", {"n": 1})            # plugin topic → local only
            bus.emit("monitoring:monitors_changed", {"n": 2}, cluster=True)  # forced
            bus.emit("plugin:state_changed", {"plugin_id": "y", ORIGIN_KEY: "peer"})  # relayed → stop
            await asyncio.sleep(0.05)
        finally:
            bridge.detach()
        bus.emit("plugin:state_changed", {"plugin_id": "after-detach"})
        await asyncio.sleep(0.02)

    asyncio.run(run())
    assert coord.published == [
        ("plugin:state_changed", {"plugin_id": "x"}),
        ("monitoring:monitors_changed", {"n": 2}),
    ]
    assert bridge.published == 2


def test_remote_events_are_re_emitted_locally_with_origin():
    coord = RecordingCoordinator()
    bridge = ClusterBridge(coord, "me")
    seen = []

    def _listener(payload):
        seen.append(payload)

    _listener.__qualname__ = f"bridge_test_listener<{id(seen)}>"
    bus.subscribe("ui:needs_refresh")(_listener)

    async def run():
        bridge.attach()
        try:
            assert len(coord.callbacks) == 1
            coord.callbacks[0]("ui:needs_refresh", {"reason": "peer installed"}, "peer-1")
            coord.callbacks[0]("ui:needs_refresh", {"reason": "echo"}, "me")  # own origin → ignored
            await asyncio.sleep(0.05)
        finally:
            bridge.detach()
            bus.unsubscribe("ui:needs_refresh", _listener)

    asyncio.run(run())
    assert seen == [{"reason": "peer installed", ORIGIN_KEY: "peer-1"}]
    assert bridge.relayed == 1
    # The relayed event must not have been published back out.
    assert coord.published == []
