"""Coordinator contract shared by every driver (``local``, ``db``; ``redis`` later).

A coordinator answers four questions for the instances of one Lyndrix
deployment: *who is alive* (instances + heartbeat), *who holds what* (leases),
*what is the shared short-lived state* (TTL key/value) and *what happened
elsewhere* (publish/subscribe of bridged bus topics). Everything above it —
the leader lease, plugin placement, the SSE relay — is driver-agnostic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Protocol, Union

# callback(topic, payload, origin_instance_id) — sync or async
EventCallback = Callable[[str, dict, str], Union[Awaitable[None], None]]


@dataclass
class InstanceInfo:
    instance_id: str
    hostname: Optional[str]
    roles: list[str]
    core_version: Optional[str]
    api_version: Optional[str]
    started_at: Optional[datetime]
    heartbeat_at: Optional[datetime]
    age_s: float            # seconds since the last heartbeat (DB clock)
    alive: bool             # age_s <= lease TTL
    is_self: bool
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("started_at", "heartbeat_at"):
            value = data.get(key)
            data[key] = value.isoformat() if isinstance(value, datetime) else value
        data["age_s"] = round(self.age_s, 1)
        return data


@dataclass
class LeaseInfo:
    name: str
    holder: Optional[str]
    acquired_at: Optional[datetime]
    expires_at: Optional[datetime]
    remaining_s: float      # negative when expired
    expired: bool

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("acquired_at", "expires_at"):
            value = data.get(key)
            data[key] = value.isoformat() if isinstance(value, datetime) else value
        data["remaining_s"] = round(self.remaining_s, 1)
        return data


class Coordinator(Protocol):
    driver: str
    instance_id: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # --- instances ---
    async def heartbeat(self, meta: dict) -> None: ...
    async def instances(self) -> list[InstanceInfo]: ...

    # --- leases ---
    async def try_acquire(self, name: str, ttl: Optional[int] = None) -> bool: ...
    async def release(self, name: str) -> bool: ...
    async def force_release(self, name: str) -> bool: ...
    async def holder(self, name: str) -> Optional[LeaseInfo]: ...
    async def leases(self) -> list[LeaseInfo]: ...

    # --- TTL key/value ---
    async def kv_set(self, key: str, value: Any, ttl: Optional[int] = None) -> None: ...
    async def kv_get(self, key: str) -> Any: ...
    async def kv_delete(self, key: str) -> bool: ...
    async def kv_incr(self, key: str, ttl: Optional[int] = None) -> int: ...

    # --- events ---
    async def publish(self, topic: str, payload: dict) -> None: ...
    def subscribe(self, callback: EventCallback) -> None: ...

    # --- diagnostics ---
    async def db_now(self) -> datetime: ...
    async def server_info(self) -> dict: ...


def build_coordinator(driver: str, *, instance_id: str, ttl: int, heartbeat_s: int, poll_ms: int) -> Coordinator:
    """Factory keyed by ``LYNDRIX_COORDINATOR``."""
    driver = (driver or "local").lower()
    if driver == "local":
        from .drivers.local import LocalCoordinator

        return LocalCoordinator(instance_id=instance_id, ttl=ttl)
    if driver == "db":
        from .drivers.db import DbCoordinator

        return DbCoordinator(instance_id=instance_id, ttl=ttl, heartbeat_s=heartbeat_s, poll_ms=poll_ms)
    raise ValueError(f"Unknown coordinator driver '{driver}' (expected local or db)")
