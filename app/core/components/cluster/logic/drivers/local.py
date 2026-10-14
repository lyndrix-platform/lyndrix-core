"""``local`` driver — the single-instance coordinator.

This instance is the only member: every lease is granted to it, the key/value
store lives in memory and ``publish`` is a no-op (the local bus already
delivered the event). It exists so plugins and core components write **one**
code path (``ctx.on_lease``, ``ctx.emit(..., cluster=True)``) that behaves
identically in ``single`` and ``cluster`` mode.
"""
from __future__ import annotations

import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..coordinator import EventCallback, InstanceInfo, LeaseInfo


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LocalCoordinator:
    driver = "local"

    def __init__(self, *, instance_id: str, ttl: int) -> None:
        self.instance_id = instance_id
        self.ttl = int(ttl)
        self._started_at = _utcnow()
        self._heartbeat_at = self._started_at
        self._meta: dict = {}
        self._leases: dict[str, tuple[datetime, datetime]] = {}  # name → (acquired, expires)
        self._kv: dict[str, tuple[Any, Optional[float]]] = {}    # key → (value, expires_monotonic)
        self._subscribers: list[EventCallback] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False
        self._leases.clear()

    # --- instances ---

    async def heartbeat(self, meta: dict) -> None:
        self._heartbeat_at = _utcnow()
        self._meta = dict(meta or {})

    async def instances(self) -> list[InstanceInfo]:
        from config import settings
        from core.api import __api_version__
        from version import __version__

        return [
            InstanceInfo(
                instance_id=self.instance_id,
                hostname=socket.gethostname(),
                roles=list(settings.instance_roles),
                core_version=__version__,
                api_version=__api_version__,
                started_at=self._started_at,
                heartbeat_at=self._heartbeat_at,
                age_s=(_utcnow() - self._heartbeat_at).total_seconds(),
                alive=True,
                is_self=True,
                meta=dict(self._meta),
            )
        ]

    # --- leases ---

    async def try_acquire(self, name: str, ttl: Optional[int] = None) -> bool:
        now = _utcnow()
        acquired = self._leases.get(name, (now, now))[0]
        self._leases[name] = (acquired, now + timedelta(seconds=int(ttl or self.ttl)))
        return True

    async def release(self, name: str) -> bool:
        return self._leases.pop(name, None) is not None

    async def force_release(self, name: str) -> bool:
        return await self.release(name)

    async def holder(self, name: str) -> Optional[LeaseInfo]:
        entry = self._leases.get(name)
        if entry is None:
            return None
        acquired, expires = entry
        remaining = (expires - _utcnow()).total_seconds()
        return LeaseInfo(name=name, holder=self.instance_id, acquired_at=acquired, expires_at=expires,
                         remaining_s=remaining, expired=remaining < 0)

    async def leases(self) -> list[LeaseInfo]:
        out = []
        for name in list(self._leases):
            info = await self.holder(name)
            if info is not None:
                out.append(info)
        return out

    # --- TTL key/value ---

    def _purge(self) -> None:
        now = time.monotonic()
        for key in [k for k, (_, exp) in self._kv.items() if exp is not None and exp <= now]:
            self._kv.pop(key, None)

    async def kv_set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._purge()
        self._kv[key] = (value, time.monotonic() + ttl if ttl else None)

    async def kv_get(self, key: str) -> Any:
        self._purge()
        entry = self._kv.get(key)
        return None if entry is None else entry[0]

    async def kv_delete(self, key: str) -> bool:
        return self._kv.pop(key, None) is not None

    async def kv_incr(self, key: str, ttl: Optional[int] = None) -> int:
        self._purge()
        entry = self._kv.get(key)
        if entry is None:
            value, expires = 0, (time.monotonic() + ttl if ttl else None)
        else:
            value, expires = int(entry[0] or 0), entry[1]
        value += 1
        self._kv[key] = (value, expires)
        return value

    # --- events ---

    async def publish(self, topic: str, payload: dict) -> None:
        # Nothing to do: the local bus delivered the event to every subscriber
        # of this (only) instance already.
        return None

    def subscribe(self, callback: EventCallback) -> None:
        self._subscribers.append(callback)

    # --- diagnostics ---

    async def db_now(self) -> datetime:
        return _utcnow()

    async def server_info(self) -> dict:
        return {"driver": self.driver}
