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

# Lifecycle states of an instance as its peers see it:
#   alive       heart-beating within the lease TTL
#   draining    heart-beating, but it announced that it is shutting down
#   restarting  stopped on purpose a moment ago (tombstone, or it went silent
#               while draining) and is expected back within the restart grace
#   dead        no heartbeat for a TTL without having said goodbye, or the
#               restart grace is over
# ("unknown" is not a state of the instance but of the observer: its own view
# of the registry is missing or stale — ClusterService.instance_state.)
INSTANCE_STATES = ("alive", "draining", "restarting", "dead")


class InstanceIdConflict(RuntimeError):
    """Another running process registered under this instance id after us."""


def instance_state(*, age_s: float, ttl: float, grace_s: float, draining: bool,
                   stopped_for_s: Optional[float]) -> str:
    """Classify a registry row. ``stopped_for_s`` is the age of its tombstone
    (None = the row is not a tombstone)."""
    if stopped_for_s is not None:
        return "restarting" if stopped_for_s <= grace_s else "dead"
    if age_s <= ttl:
        return "draining" if draining else "alive"
    # Silent after it announced the drain: it was stopping on purpose and was
    # killed before it could leave a tombstone (container stop timeout).
    if draining and age_s <= grace_s:
        return "restarting"
    return "dead"


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
    alive: bool             # heart-beating within the lease TTL (state alive or draining)
    is_self: bool
    meta: dict = field(default_factory=dict)
    # Lifecycle state (core ≥ 1.12), see INSTANCE_STATES. Derived from ``alive``
    # when a driver/test does not set it.
    state: str = ""
    # Boot nonce of the process that owns the row: a restart under a fixed
    # instance id shows as a new incarnation. None for rows of core ≤ 1.11.
    incarnation: Optional[str] = None
    stopped_at: Optional[datetime] = None   # set while the row is a tombstone of a graceful stop

    def __post_init__(self) -> None:
        if not self.state:
            self.state = "alive" if self.alive else "dead"

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("started_at", "heartbeat_at", "stopped_at"):
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
    token: Optional[int] = None   # fencing token of the current acquisition (drivers with tokens)

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
    async def try_acquire(self, name: str, ttl: Optional[float] = None) -> bool: ...
    # Optional (drivers with fencing tokens — ``local`` and ``db`` have it):
    #   async def acquire(name, ttl=None, *, fresh=False) -> Optional[int]
    # returns the lease's token (None = held by a peer); ``fresh=True`` forces a
    # new acquisition with a new token. The LeaseKeeper falls back to
    # ``try_acquire`` (no token) when a driver lacks it.
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
