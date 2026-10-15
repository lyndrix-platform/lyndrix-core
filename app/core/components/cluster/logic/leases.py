"""LeaseKeeper — wants, renews and hands over named leases.

A *lease* is the cluster's "exactly one instance does this" primitive: the
core leader (``core:leader``), a plugin's background worker
(``monitoring:prober``), a singleton plugin (``plugin:<id>:singleton``).
Callers register what they *want*; the keeper tries to acquire every wanted
lease on each heartbeat, renews the ones it holds and fires ``acquired`` /
``lost`` callbacks on transitions.

Fencing: when the coordinator is unreachable the keeper does **not** know
whether it still holds a lease. It keeps acting as holder until the lease TTL
has elapsed since the last successful renewal, then declares the lease lost —
by that time a peer may legitimately own it, so the local worker must stop.
"""
from __future__ import annotations

import asyncio
import inspect
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.bus import bus
from core.logger import get_logger

from .coordinator import Coordinator

log = get_logger("Core:Cluster:Leases")

# Bridged on purpose (emit(..., cluster=True)): "I gave lease X up — take it."
RELEASED_TOPIC = "cluster:lease_released"

Callback = Optional[Callable[[], object]]


@dataclass
class _Wanted:
    ttl: int
    callbacks: list[tuple[Callback, Callback]] = field(default_factory=list)  # (acquired, lost)


class LeaseKeeper:
    def __init__(self, coordinator: Coordinator, *, ttl: int, heartbeat_s: int) -> None:
        self._coordinator = coordinator
        self.ttl = int(ttl)
        self.heartbeat_s = int(heartbeat_s)
        self._wanted: dict[str, _Wanted] = {}
        self._held: dict[str, float] = {}  # name → monotonic time of the last successful renewal
        self._task: Optional[asyncio.Task] = None
        self._kick = asyncio.Event()
        self._running = False
        self._lock = asyncio.Lock()
        self.on_change: Optional[Callable[[str, bool], None]] = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def held(self) -> list[str]:
        return sorted(self._held)

    @property
    def wanted(self) -> list[str]:
        return sorted(self._wanted)

    def holds(self, name: str) -> bool:
        return name in self._held

    def want(self, name: str, *, acquired: Callback = None, lost: Callback = None, ttl: Optional[int] = None) -> None:
        """Register interest in ``name``; ``acquired``/``lost`` fire on transitions.

        If the lease is already held when a new callback registers, ``acquired``
        fires right away (a plugin that starts late still gets its signal).
        """
        entry = self._wanted.get(name)
        if entry is None:
            entry = self._wanted[name] = _Wanted(ttl=int(ttl or self.ttl))
        if acquired is not None or lost is not None:
            entry.callbacks.append((acquired, lost))
            if name in self._held:
                self._fire(acquired, name, "acquired")
        self._kick.set()

    def forget(self, name: str, *, acquired: Callback = None, lost: Callback = None,
               fire_lost: bool = False) -> None:
        """Drop a callback pair (or every interest in ``name`` when none given).

        When the last interest goes, a held lease is released so a peer can
        take over immediately instead of after the TTL. By default the ``lost``
        callback is **not** fired for the removed pair — the owner is tearing
        down. ``fire_lost=True`` (``ctx.release_lease``) fires it when the lease
        was held, so the owner's single "stop the work" path runs and a peer
        is told to take over at once.
        """
        entry = self._wanted.get(name)
        if entry is None:
            return
        if acquired is None and lost is None:
            removed = list(entry.callbacks)
            entry.callbacks.clear()
        else:
            removed = [cb for cb in entry.callbacks if cb == (acquired, lost)]
            entry.callbacks = [cb for cb in entry.callbacks if cb != (acquired, lost)]
        was_held = name in self._held
        if not entry.callbacks:
            self._wanted.pop(name, None)
            if was_held:
                self._held.pop(name, None)
                self._release_in_background(name)
                self._notify(name, False)
        if fire_lost and was_held:
            for _acquired, lost_cb in removed:
                self._fire(lost_cb, name, "lost")

    def _release_in_background(self, name: str) -> None:
        coro = self._release_and_announce(name)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Called from sync code without a loop (tests, sync teardown paths):
            # run the release to completion right here.
            try:
                asyncio.run(coro)
            except Exception as exc:
                log.debug("LEASE: synchronous release of '%s' failed: %s", name, exc)
            return
        bus.create_tracked_task(coro, name=f"cluster:lease_release:{name}")

    async def release_now(self, name: str) -> bool:
        """Give a held lease up explicitly (admin action, drain) and fire ``lost``."""
        if name not in self._held:
            return False
        self._held.pop(name, None)
        try:
            await self._coordinator.release(name)
        except Exception as exc:
            log.warning("LEASE: release of '%s' failed: %s", name, exc)
        self._fire_all(name, "lost")
        self._notify(name, False)
        # Tell the peers (bridged topic) so one of them acquires it on the spot
        # instead of on its next heartbeat — a graceful leader hand-over takes
        # about a second rather than up to one heartbeat interval.
        try:
            bus.emit(RELEASED_TOPIC, {"name": name}, cluster=True)
        except Exception:
            log.debug("LEASE: release broadcast failed", exc_info=True)
        return True

    def kick(self) -> None:
        """Run the acquire pass as soon as possible (a peer released a lease)."""
        self._kick.set()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = bus.create_tracked_task(self._loop(), name="cluster:lease_keeper")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        for name in list(self._held):
            await self.release_now(name)

    async def tick(self) -> None:
        """One acquire/renew pass over every wanted lease (also used by tests)."""
        async with self._lock:
            for name, entry in list(self._wanted.items()):
                try:
                    ok: Optional[bool] = await self._coordinator.try_acquire(name, entry.ttl)
                except Exception as exc:
                    ok = None
                    log.debug("LEASE: try_acquire('%s') failed: %s", name, exc)
                now = time.monotonic()
                was_held = name in self._held
                if ok is True:
                    self._held[name] = now
                    if not was_held:
                        log.info("LEASE: acquired '%s'", name)
                        self._fire_all(name, "acquired")
                        self._notify(name, True)
                elif ok is False:
                    if was_held:
                        log.warning("LEASE: lost '%s' to a peer", name)
                        self._held.pop(name, None)
                        self._fire_all(name, "lost")
                        self._notify(name, False)
                else:  # coordinator unreachable → fence after TTL
                    if was_held and now - self._held[name] > entry.ttl:
                        log.error("LEASE: '%s' could not be renewed for %ss — treating it as lost", name, entry.ttl)
                        self._held.pop(name, None)
                        self._fire_all(name, "lost")
                        self._notify(name, False)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("LEASE: keeper tick failed", exc_info=True)
            self._kick.clear()
            # Jitter keeps N instances from hammering the DB in lock-step.
            delay = self.heartbeat_s * random.uniform(0.8, 1.0)
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _release_quietly(self, name: str) -> None:
        try:
            await self._coordinator.release(name)
        except Exception as exc:
            log.debug("LEASE: quiet release of '%s' failed: %s", name, exc)

    async def _release_and_announce(self, name: str) -> None:
        """Release at the coordinator, then tell the peers so one of them
        acquires the lease on the spot instead of on its next heartbeat."""
        await self._release_quietly(name)
        try:
            bus.emit(RELEASED_TOPIC, {"name": name}, cluster=True)
        except Exception:
            log.debug("LEASE: release broadcast failed", exc_info=True)

    def _fire_all(self, name: str, event: str) -> None:
        entry = self._wanted.get(name)
        if entry is None:
            return
        for acquired, lost in list(entry.callbacks):
            self._fire(acquired if event == "acquired" else lost, name, event)

    @staticmethod
    def _fire(callback: Callback, name: str, event: str) -> None:
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                bus.create_tracked_task(result, name=f"cluster:lease_{event}:{name}")
        except Exception:
            log.error("LEASE: %s callback for '%s' raised", event, name, exc_info=True)

    def _notify(self, name: str, held: bool) -> None:
        if self.on_change is not None:
            try:
                self.on_change(name, held)
            except Exception:
                log.debug("LEASE: on_change hook failed", exc_info=True)
