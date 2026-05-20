import asyncio
import inspect
from typing import Dict, List, Callable, Set
from core.logger import get_logger


class GlobalEventBus:
    def __init__(self):
        self.subscribers: Dict[str, List[Callable]] = {}
        self._subscriber_keys: Dict[str, Set[str]] = {}
        self.log = get_logger("Core:EventBus")
        self._active_tasks: Set[asyncio.Task] = set()
        # Topics that should not spam INFO logs
        self._noise_topics = ["system:metrics_update"]
        # Topics with sensitive payloads (never log data)
        self._sensitive_topics = ["vault:unseal_requested", "vault:init_requested"]
        # Topics with very large payloads should be summarized to keep logs usable.
        self._summarized_topics = {
            "monitoring:inventory_sync",
            "monitoring:state_changed",
        }

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

    def subscribe(self, topic: str):
        """Decorator: @bus.subscribe('topic') registers a callback."""

        def decorator(callback):
            if topic not in self.subscribers:
                self.subscribers[topic] = []
                self._subscriber_keys[topic] = set()

            callback_key = (
                f"{getattr(callback, '__module__', 'unknown')}:"
                f"{getattr(callback, '__qualname__', callback.__name__)}"
            )
            if callback_key in self._subscriber_keys[topic]:
                self.log.debug(
                    f"SUBSCRIBE: Ignored duplicate subscriber for {topic} ({callback.__name__})"
                )
                return callback
            self.subscribers[topic].append(callback)
            self._subscriber_keys[topic].add(callback_key)
            self.log.debug(f"SUBSCRIBE: Registered for: {topic} ({callback.__name__})")
            return callback

        return decorator

    def emit(self, topic: str, payload: dict = None):
        """Dispatches an event to all subscribers, tracking async tasks."""
        if payload is None:
            payload = {}

        if topic == "system:metrics_update":
            self.log.debug(f"METRICS: {topic}")
        elif topic in self._sensitive_topics:
            self.log.info(f"EVENT: {topic} | Data: [REDACTED]")
        elif topic in self._summarized_topics:
            self.log.info(
                f"EVENT: {topic} | Data: {self._summarize_payload(topic, payload)}"
            )
        else:
            self.log.info(f"EVENT: {topic} | Data: {payload}")

        if topic in self.subscribers:
            for callback in self.subscribers[topic]:
                try:
                    if inspect.iscoroutinefunction(callback):
                        task = asyncio.create_task(
                            callback(payload), name=f"bus:{topic}:{callback.__name__}"
                        )
                        self._track_task(task, topic, callback.__name__)
                    else:
                        callback(payload)
                except Exception as e:
                    self.log.error(
                        f"ERROR: Callback '{callback.__name__}' for '{topic}' raised: {e}",
                        exc_info=True,
                    )

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

    def create_tracked_task(self, coro, *, name: str = None) -> asyncio.Task:
        """Creates and tracks an asyncio task with failure logging.

        Use this instead of bare asyncio.create_task() for observability.
        """
        task = asyncio.create_task(coro, name=name)
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
