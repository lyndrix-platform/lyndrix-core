"""Bus bridge — fans an allowlist of bus topics out to every instance.

The ``GlobalEventBus`` stays process-local (plugins subscribe/emit exactly as
before). The bridge hooks ``bus.emit``: a topic on the core allowlist, or any
``emit(..., cluster=True)``, is also written to the coordinator; events
arriving from a peer are re-emitted on the local bus with ``payload["_origin"]
= <peer instance id>``. The ``_origin`` marker is what stops a relayed event
from being published again (no ping-pong), and lets a subscriber tell local
from remote when it matters.

Why an allowlist and not "bridge everything": the moment a topic is bridged,
every side-effecting subscriber runs on **N** instances. Plugin topics stay
local unless the plugin says ``cluster=True`` on purpose.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Optional

from core.bus import bus
from core.logger import get_logger

from .coordinator import Coordinator

log = get_logger("Core:Cluster:Bridge")

ORIGIN_KEY = "_origin"

# Set while a relayed event is being dispatched locally. Everything a handler
# emits in reaction — synchronously, or from the task/thread the bus spawns for
# it (asyncio.create_task and asyncio.to_thread copy the context) — is a
# *derived* event and must not be published again: four instances each turning
# one relayed "plugin:installed" into a fresh "notification:new" and bridging
# that produced 16 toasts per browser for one install.
RELAY_ORIGIN: ContextVar[Optional[str]] = ContextVar("lyndrix_relay_origin", default=None)

# Core-owned topics every instance (and every SSE client) must see.
CLUSTER_TOPICS = frozenset({
    "plugin:state_changed",
    "plugin:runtime_status",
    "plugin:desired_changed",
    "plugin:installed",
    "plugin:install_failed",
    "plugin:update_available",
    "plugin:settings_changed",
    "plugin:operation_changed",
    "ui:needs_refresh",
    "notification:new",
    "settings:changed",
    "auth:providers_changed",
    "theme:changed",
    "system:restart_requested",
})


class ClusterBridge:
    def __init__(self, coordinator: Coordinator, instance_id: str) -> None:
        self._coordinator = coordinator
        self.instance_id = instance_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.attached = False
        self.published = 0
        self.relayed = 0

    def attach(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self.attached:
            self._coordinator.subscribe(self._on_remote_event)
        bus.set_cluster_publisher(self._publish_hook)
        self.attached = True

    def detach(self) -> None:
        bus.set_cluster_publisher(None)
        self.attached = False

    # --- outbound: local emit → coordinator -----------------------------

    def _publish_hook(self, topic: str, payload: dict, forced: bool) -> None:
        """Called synchronously from ``bus.emit`` (possibly from a worker thread)."""
        if not self.attached or ORIGIN_KEY in payload:
            return
        if RELAY_ORIGIN.get() is not None:
            return  # derived from a relayed event — stays on this instance
        if not (forced or topic in CLUSTER_TOPICS):
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # bus.emit may run inside asyncio.to_thread code → hop onto the loop.
        loop.call_soon_threadsafe(self._schedule_publish, topic, dict(payload))

    def _schedule_publish(self, topic: str, payload: dict) -> None:
        bus.create_tracked_task(self._publish(topic, payload), name=f"cluster:publish:{topic}")

    async def _publish(self, topic: str, payload: dict) -> None:
        try:
            await self._coordinator.publish(topic, payload)
            self.published += 1
        except Exception as exc:
            log.warning("BRIDGE: publish of '%s' failed: %s", topic, exc)

    # --- inbound: coordinator → local bus -------------------------------

    def _on_remote_event(self, topic: str, payload: dict, origin: str) -> None:
        if origin == self.instance_id:
            return
        self.relayed += 1
        token = RELAY_ORIGIN.set(origin)
        try:
            bus.emit(topic, {**(payload or {}), ORIGIN_KEY: origin})
        finally:
            RELAY_ORIGIN.reset(token)
