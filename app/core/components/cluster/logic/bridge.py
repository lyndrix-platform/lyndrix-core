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

Outbound events go through ONE publisher task and a bounded outbox:

* **order** — events of this instance reach the coordinator in the order they
  were emitted (one task per event used to race: "ready" could overtake
  "starting");
* **database outage** — a failed publish stays at the head of the outbox and
  is retried with back-off; events emitted meanwhile queue behind it. They
  used to be dropped with a log line — a lost ``settings:changed`` was never
  re-converged;
* **bounds** — the outbox holds ``OUTBOX_MAX`` events (the oldest is dropped,
  counted), an event older than ``OUTBOX_MAX_AGE_S`` is not sent any more (a
  command delivered many minutes late does more harm than good), and a payload
  above ``PAYLOAD_MAX_BYTES`` is not published at all (logged, counted).

Delivery is at-least-once from the publisher's side: after an ambiguous
failure (the answer to an INSERT was lost) the event is sent again.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Optional

from core.bus import bus
from core.logger import get_logger

from .coordinator import Coordinator
# Set while a relayed event is being dispatched locally — see origin.py for
# what it suppresses and where the suppression deliberately ends.
from .origin import RELAY_ORIGIN, clean_context  # noqa: F401  (re-exported)

log = get_logger("Core:Cluster:Bridge")

ORIGIN_KEY = "_origin"

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

OUTBOX_MAX = 1000
OUTBOX_MAX_AGE_S = 300.0
PAYLOAD_MAX_BYTES = 64 * 1024


class ClusterBridge:
    def __init__(self, coordinator: Coordinator, instance_id: str) -> None:
        self._coordinator = coordinator
        self.instance_id = instance_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.attached = False
        self.published = 0
        self.relayed = 0
        # (topic, payload, monotonic time of the emit), oldest first.
        self._outbox: deque = deque()
        self._wake: Optional[asyncio.Event] = None
        self._publisher_task: Optional[asyncio.Task] = None
        # Seconds to wait before the n-th retry of a failed publish (last one repeats).
        self.retry_delays: tuple = (0.2, 0.5, 1.0, 2.0, 5.0)
        self.dropped = 0       # pushed out of a full outbox (oldest first)
        self.expired = 0       # too old when their turn came
        self.oversized = 0     # payload above PAYLOAD_MAX_BYTES — never queued
        self.failures = 0      # failed publish attempts (retried)

    def attach(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self.attached:
            self._coordinator.subscribe(self._on_remote_event)
        bus.set_cluster_publisher(self._publish_hook)
        self.attached = True
        if self._publisher_task is None or self._publisher_task.done():
            self._wake = asyncio.Event()
            # Clean context: attach() may run inside a relayed event's handler.
            self._publisher_task = bus.create_tracked_task(
                self._publisher(), name="cluster:publisher", context=clean_context())

    def detach(self) -> None:
        bus.set_cluster_publisher(None)
        self.attached = False
        if self._publisher_task is not None and not self._publisher_task.done():
            self._publisher_task.cancel()
        self._publisher_task = None

    async def flush(self, timeout: float = 2.0) -> bool:
        """Wait until the outbox is empty (shutdown: let the last lease release
        broadcasts out). Returns ``False`` when events are still queued."""
        deadline = time.monotonic() + timeout
        # Emits made just before the call are still on their way into the
        # outbox (call_soon_threadsafe) — let the loop run them first.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while self._outbox and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return not self._outbox

    def status(self) -> dict:
        return {
            "attached": self.attached, "published": self.published, "relayed": self.relayed,
            "outbox": len(self._outbox), "dropped": self.dropped, "expired": self.expired,
            "oversized": self.oversized, "failures": self.failures,
        }

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
        loop.call_soon_threadsafe(self._enqueue, topic, dict(payload))

    def _enqueue(self, topic: str, payload: dict) -> None:
        try:
            size = len(json.dumps(payload, default=str))
        except Exception:
            size = 0  # not JSON-able as it is — the driver stores a string form
        if size > PAYLOAD_MAX_BYTES:
            self.oversized += 1
            log.warning(
                "BRIDGE: '%s' not published to the cluster — payload is %d kB (limit %d kB). Bridged events are "
                "signals ('my data changed, re-read it'), not data transport; peers did not receive this one.",
                topic, size // 1024, PAYLOAD_MAX_BYTES // 1024,
            )
            return
        self._outbox.append((topic, payload, time.monotonic()))
        if len(self._outbox) > OUTBOX_MAX:
            self._outbox.popleft()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.error("BRIDGE: outbox full (%d events) — %d event(s) dropped so far, oldest first; "
                          "the coordinator has been unreachable for too long", OUTBOX_MAX, self.dropped)
        if self._wake is not None:
            self._wake.set()

    async def _publisher(self) -> None:
        """Send the outbox head first, one event at a time; keep it there until
        the coordinator took it."""
        attempt = 0
        while True:
            if not self._outbox:
                self._wake.clear()
                await self._wake.wait()
                continue
            entry = self._outbox[0]
            topic, payload, queued_at = entry
            if time.monotonic() - queued_at > OUTBOX_MAX_AGE_S:
                self._drop_head(entry)
                self.expired += 1
                log.warning("BRIDGE: '%s' waited more than %.0fs for the coordinator — not sent any more",
                            topic, OUTBOX_MAX_AGE_S)
                continue
            try:
                await self._coordinator.publish(topic, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures += 1
                delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                if attempt in (0, 5) or attempt % 50 == 0:
                    log.warning("BRIDGE: publish of '%s' failed (%s) — %d event(s) queued, retrying in %.1fs",
                                topic, exc, len(self._outbox), delay)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            if attempt:
                log.info("BRIDGE: coordinator reachable again — sending %d queued event(s)", len(self._outbox))
            attempt = 0
            self._drop_head(entry)
            self.published += 1

    def _drop_head(self, entry: tuple) -> None:
        # The head may have been pushed out by a full outbox while we were sending it.
        if self._outbox and self._outbox[0] is entry:
            self._outbox.popleft()

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
