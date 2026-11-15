import asyncio
import inspect
import threading
from typing import Dict, List, Callable, Optional, Set
from core.logger import get_logger, mask_secrets


class GlobalEventBus:
    def __init__(self):
        self.subscribers: Dict[str, List[Callable]] = {}
        self.log = get_logger("Core:EventBus")
        self._active_tasks: Set[asyncio.Task] = set()
        # The application loop and its thread, learned from the first emit() that
        # runs on a loop (system:started). emit() from a worker thread
        # (asyncio.to_thread writers, hvac/DB helpers) is handed over to this
        # loop instead of calling asyncio.create_task() where there is none.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[int] = None
        # Topics that should not spam INFO logs
        self._noise_topics = ["system:metrics_update"]
        # Topics with sensitive payloads (never log data)
        self._sensitive_topics = ["vault:unseal_requested", "vault:init_requested"]
        # Topics with very large payloads should be summarized to keep logs usable.
        self._summarized_topics = {
            "monitoring:inventory_sync",
            "monitoring:state_changed",
        }
        # Cluster bridge hook (core.components.cluster.logic.bridge): called after
        # the local dispatch with (topic, payload, forced). None in tests / before
        # the coordinator attached — the bus itself stays process-local.
        self._cluster_publisher: Callable | None = None

    def set_cluster_publisher(self, publisher: Callable | None) -> None:
        """Install (or remove) the cluster bridge's publish hook."""
        self._cluster_publisher = publisher

    def _summarize_payload(self, topic: str, payload: dict) -> str:
        if topic == "monitoring:inventory_sync":
            return (
                "{"
                f"'owner_source': {payload.get('owner_source')!r}, "
                f"'source_revision': {payload.get('source_revision')!r}, "
                f"'hosts': {len(payload.get('hosts') or [])}, "
                f"'services': {len(payload.get('services') or [])}"
                "}"
            )
        if topic == "monitoring:state_changed":
            transition = f"{payload.get('previous_state')}->{payload.get('new_state')}"
            return (
                "{"
                f"'monitor_id': {payload.get('monitor_id')!r}, "
                f"'transition': {transition!r}, "
                f"'error_message': {payload.get('error_message')!r}"
                "}"
            )
        return str(payload)

    @staticmethod
    def _callback_name(callback) -> str:
        """Readable name for logs and task names (partials/callable objects have no ``__name__``)."""
        return getattr(callback, "__name__", None) or type(callback).__name__

    def subscribe(self, topic: str):
        """Decorator: @bus.subscribe('topic') registers a callback.

        Subscribing the *same* callback twice is a no-op. "Same" means equal:
        the same function object, or a bound method of the same instance. Two
        instances' bound methods, closures produced by a factory and lambdas are
        distinct subscribers and are all delivered — the bus used to de-duplicate
        by ``module:qualname`` and silently dropped all but the first of them.

        A reloaded module creates new function objects, so the subscriber is
        responsible for unsubscribing the old ones: ``ModuleContext.dispose()``
        does it for everything subscribed through ``ctx.subscribe`` and the
        ModuleManager calls ``unsubscribe_package`` when it unloads a plugin.
        """

        def decorator(callback):
            subs = self.subscribers.setdefault(topic, [])
            if any(existing == callback for existing in subs):
                self.log.debug(
                    f"SUBSCRIBE: Ignored duplicate subscriber for {topic} ({self._callback_name(callback)})"
                )
                return callback
            subs.append(callback)
            self.log.debug(f"SUBSCRIBE: Registered for: {topic} ({self._callback_name(callback)})")
            return callback

        return decorator

    def unsubscribe(self, topic: str, callback) -> bool:
        """Remove a previously registered callback.

        Called on plugin unload/disable so the old handler stops firing. Matching
        is by equality, like ``subscribe``: ``bus.unsubscribe(topic, obj.method)``
        works although every attribute access creates a new bound-method object.
        """
        subs = self.subscribers.get(topic)
        if not subs:
            return False
        kept = [cb for cb in subs if cb != callback]
        self.subscribers[topic] = kept
        return len(kept) != len(subs)

    def unsubscribe_package(self, package: str) -> int:
        """Remove every subscriber defined in ``package`` or one of its submodules.

        Safety net for an unloaded plugin: handlers it subscribed directly on the
        bus (not through its context) would otherwise keep firing from the purged
        module — and fire twice once the re-imported code subscribes again.
        Returns the number of subscribers removed.
        """
        def _owned(callback) -> bool:
            module = str(getattr(callback, "__module__", "") or "")
            return module == package or module.startswith(package + ".")

        removed = 0
        for topic, subs in self.subscribers.items():
            kept = [cb for cb in subs if not _owned(cb)]
            removed += len(subs) - len(kept)
            self.subscribers[topic] = kept
        return removed

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the application loop (and its thread) emit() dispatches on.

        Called automatically by the first emit() that runs on a loop; the
        startup hook may call it explicitly. ``None`` forgets the binding
        (tests that run several loops).
        """
        if loop is None:
            self._loop, self._loop_thread = None, None
            return
        self._loop, self._loop_thread = loop, threading.get_ident()

    def _off_loop(self) -> bool:
        """True when called from a thread that is not the bound loop's thread."""
        if self._loop is None or self._loop.is_closed():
            return False
        return threading.get_ident() != self._loop_thread

    def emit(self, topic: str, payload: dict = None, *, cluster: bool = False):
        """Dispatches an event to all subscribers, tracking async tasks.

        ``cluster=True`` additionally publishes the event to every other core
        instance through the cluster bridge (core-owned topics on the bridge's
        allowlist are published regardless). A relayed event arrives on the
        peers' local buses with ``payload["_origin"] = <sender instance id>``
        and is never re-published. In ``single`` mode the flag is a no-op.

        Thread-safe: an emit from a worker thread (``asyncio.to_thread`` code,
        blocking Vault/DB helpers) is scheduled onto the application loop with
        ``call_soon_threadsafe`` — subscribers always run on the loop, sync ones
        included, and async ones get their task created where a loop exists.
        """
        if payload is None:
            payload = {}

        if self._off_loop():
            self._loop.call_soon_threadsafe(self._emit_on_loop, topic, payload, cluster)
            return
        if self._loop is None or self._loop.is_closed():
            try:
                self.bind_loop(asyncio.get_running_loop())
            except RuntimeError:
                pass  # no loop at all (unit tests, sync bootstrap) — dispatch inline
        self._emit_on_loop(topic, payload, cluster)

    def _emit_on_loop(self, topic: str, payload: dict, cluster: bool) -> None:

        if topic == "system:metrics_update":
            self.log.debug(f"METRICS: {topic}")
        elif topic in self._sensitive_topics:
            self.log.info(f"EVENT: {topic} | Data: [REDACTED]")
        elif topic in self._summarized_topics:
            self.log.info(
                f"EVENT: {topic} | Data: {self._summarize_payload(topic, payload)}"
            )
        else:
            # Redact sensitive keys by name before stringifying the payload so
            # secrets/PII in arbitrary topics never reach the logs verbatim.
            self.log.info(f"EVENT: {topic} | Data: {mask_secrets(payload)}")

        if topic in self.subscribers:
            # Iterate over a copy: a handler may subscribe/unsubscribe while the
            # event is being dispatched.
            for callback in list(self.subscribers[topic]):
                name = self._callback_name(callback)
                try:
                    if inspect.iscoroutinefunction(callback):
                        task = asyncio.create_task(
                            callback(payload), name=f"bus:{topic}:{name}"
                        )
                        self._track_task(task, topic, name)
                    else:
                        callback(payload)
                except Exception as e:
                    self.log.error(
                        f"ERROR: Callback '{name}' for '{topic}' raised: {e}",
                        exc_info=True,
                    )

        publisher = self._cluster_publisher
        if publisher is not None:
            try:
                publisher(topic, payload, cluster)
            except Exception as e:
                self.log.error(f"ERROR: cluster publish hook for '{topic}' raised: {e}", exc_info=True)

    def _track_task(self, task: asyncio.Task, topic: str, callback_name: str):
        """Tracks an async task and logs failures via done callback."""
        self._active_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._active_tasks.discard(t)
            if t.cancelled():
                self.log.debug(f"TASK_CANCELLED: {topic}:{callback_name}")
                return
            exc = t.exception()
            if exc:
                self.log.error(
                    f"TASK_FAILED: Async handler '{callback_name}' for '{topic}' raised: {exc}",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_on_done)

    def create_tracked_task(self, coro, *, name: str = None, context=None) -> asyncio.Task:
        """Creates and tracks an asyncio task with failure logging.

        Use this instead of bare asyncio.create_task() for observability.
        ``context`` (a ``contextvars.Context``) runs the task in that context
        instead of a copy of the caller's.
        """
        task = asyncio.create_task(coro, name=name, context=context)
        self._active_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._active_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self.log.error(
                    f"TASK_FAILED: '{name or 'unnamed'}' raised: {exc}",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_on_done)
        return task


bus = GlobalEventBus()
