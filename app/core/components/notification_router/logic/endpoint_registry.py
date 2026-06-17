"""In-memory registry of declared notification endpoints.

Populated eagerly during ``ModuleManager.load_module`` (so endpoints are
known before ``db:connected`` and ``setup()`` calls), and re-synced when the
router service runs ``sync_declared_endpoints`` on ``db:connected``.
"""
from __future__ import annotations

from threading import RLock
from typing import Iterable

from core.components.plugins.logic.models import NotificationEndpoint


class EndpointRegistry:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], NotificationEndpoint] = {}
        self._lock = RLock()

    def upsert(self, plugin_id: str, endpoint: NotificationEndpoint) -> None:
        with self._lock:
            self._by_key[(plugin_id, endpoint.name)] = endpoint

    def remove_plugin(self, plugin_id: str) -> None:
        with self._lock:
            for key in [k for k in self._by_key if k[0] == plugin_id]:
                del self._by_key[key]

    def remove(self, plugin_id: str, endpoint_name: str) -> None:
        with self._lock:
            self._by_key.pop((plugin_id, endpoint_name), None)

    def get(self, plugin_id: str, endpoint_name: str) -> NotificationEndpoint | None:
        with self._lock:
            return self._by_key.get((plugin_id, endpoint_name))

    def all_for_plugin(self, plugin_id: str) -> list[NotificationEndpoint]:
        with self._lock:
            return [ep for (pid, _), ep in self._by_key.items() if pid == plugin_id]

    def plugin_ids(self) -> set[str]:
        with self._lock:
            return {pid for (pid, _) in self._by_key.keys()}

    def iter_all(self) -> Iterable[tuple[str, NotificationEndpoint]]:
        """Yield ``(plugin_id, endpoint)`` tuples — the dict's composite keys
        are flattened so callers don't have to unpack ``((pid, name), ep)``.
        """
        with self._lock:
            return [(plugin_id, endpoint) for (plugin_id, _name), endpoint in self._by_key.items()]


endpoint_registry = EndpointRegistry()
