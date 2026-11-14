"""LeaseKeeper — wants, renews and hands over named leases.

A *lease* is the cluster's "exactly one instance does this" primitive: the
core leader (``core:leader``), a plugin's background worker
(``monitoring:prober``), a singleton plugin (``plugin:<id>:singleton``).
Callers register what they *want*; the keeper tries to acquire every wanted
lease on each heartbeat, renews the ones it holds and fires ``acquired`` /
``lost`` callbacks on transitions.

Safety rules (the order of events is the whole point of a lease):

* **Self-fencing deadline.** A holding is valid until ``since + ttl − margin``
  on *this* instance's monotonic clock, where ``since`` is the moment the last
  successful acquire/renew was **started**. The coordinator stamps the expiry
  after that moment (database clock + ttl), so the local deadline always lies
  before the earliest time a peer can take the lease — by at least ``margin``.
* **``lost`` fires at the deadline, not when a tick happens to get there.** A
  watchdog task wakes at the earliest deadline of all holdings; every tick
  checks the deadlines before it tries to renew anything (an event loop that
  was paused longer than the TTL fences first and re-acquires afterwards).
* **Renewals are independent.** Each lease is renewed by its own attempt; one
  call hanging on a dead database socket delays neither the other leases nor
  the fencing of its own. At most ``MAX_INFLIGHT`` attempts per lease are in
  flight, so hung calls cannot pile up threads.
* **A late answer never resurrects a lease.** Every local transition bumps the
  lease's generation; an attempt that started before a ``lost`` / ``forget`` /
  release is ignored when it finally returns.
* **``lost`` before release.** On an explicit hand-over (``release_now``,
  drain, ``ctx.release_lease``) the holder's ``lost`` callbacks run — and async
  ones are awaited for a bounded time — before the lease is released at the
  coordinator, so a peer's ``acquired`` cannot overtake them.
* **Fencing token.** Every acquisition carries a token that only grows (per
  lease name, cluster-wide). The local deadline cannot stop work that is
  already running in a thread or subprocess when the event loop stalls; a
  plugin that stamps its writes with the token (``… WHERE token <= :mine``)
  makes the stale holder's late writes fail. See ``docs/ha/plugin-development.md``.
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

Callback = Optional[Callable[..., object]]


@dataclass
class _Wanted:
    ttl: float
    callbacks: list[tuple[Callback, Callback]] = field(default_factory=list)  # (acquired, lost)


@dataclass
class _Holding:
    since: float            # clock() at the START of the last successful acquire/renew
    ttl: float
    token: Optional[int]    # fencing token of this acquisition (None: driver without tokens)


def _accepts_token(callback: Callable[..., object]) -> Optional[str]:
    """How (if at all) ``callback`` takes the fencing token.

    ``"keyword"`` when it declares a parameter named ``token``, ``"positional"``
    when it has a required positional parameter (a call without arguments
    would fail anyway), else ``None``. Callbacks written for older cores take
    no arguments and are called exactly as before; an *optional* parameter with
    another name is never filled — it may mean something else.
    """
    try:
        params = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return None
    for param in params:
        if param.name == "token" and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            return "keyword"
    for param in params:
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD) and param.default is param.empty:
            return "positional"
    return None


class LeaseKeeper:
    # Attempts (threads inside the coordinator) allowed per lease at a time: the
    # current one plus one that is still stuck from the previous round.
    MAX_INFLIGHT = 2
    # How long an explicit hand-over waits for async ``lost`` callbacks before
    # it releases the lease anyway.
    LOST_CALLBACK_TIMEOUT_S = 5.0

    def __init__(self, coordinator: Coordinator, *, ttl: float, heartbeat_s: float,
                 clock: Callable[[], float] = time.monotonic, min_margin_s: float = 2.0) -> None:
        self._coordinator = coordinator
        self.ttl = ttl
        self.heartbeat_s = heartbeat_s
        self._clock = clock
        # A driver whose instance is the only member (``local``, single mode)
        # opts out of self-fencing: nobody can take a lease over, so a stalled
        # event loop must not bounce every worker through lost → acquired.
        self._fencing = bool(getattr(coordinator, "self_fencing", True))
        self._min_margin_s = float(min_margin_s)
        self._wanted: dict[str, _Wanted] = {}
        self._held: dict[str, _Holding] = {}
        # name → counter bumped on every local transition (acquired, lost,
        # forgotten, released). An attempt only counts if it is unchanged.
        self._generation: dict[str, int] = {}
        self._inflight: dict[str, set] = {}
        self._task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        # Loop-bound primitives are created in start() / on first use: the
        # keeper object outlives event loops in tests.
        self._kick: Optional[asyncio.Event] = None
        self._deadlines_changed: Optional[asyncio.Event] = None
        self._tick_lock: Optional[tuple] = None
        self._running = False
        self._stopped = False
        self.on_change: Optional[Callable[[str, bool], None]] = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def held(self) -> list[str]:
        return sorted(name for name in self._held if self.holds(name))

    @property
    def wanted(self) -> list[str]:
        return sorted(self._wanted)

    def holds(self, name: str) -> bool:
        """``True`` while this instance may act as holder of ``name``.

        Deadline-aware and safe to call from any thread: once the self-fencing
        deadline has passed the answer is ``False`` even if the ``lost``
        callback has not run yet (stalled event loop).
        """
        holding = self._held.get(name)
        return holding is not None and not self._expired(holding)

    def token(self, name: str) -> Optional[int]:
        """Fencing token of the current holding of ``name`` (``None`` when the
        lease is not held here or the coordinator does not issue tokens)."""
        holding = self._held.get(name)
        if holding is None or self._expired(holding):
            return None
        return holding.token

    def _expired(self, holding: _Holding) -> bool:
        return self._fencing and self._clock() >= self._valid_until(holding)

    def margin(self, ttl: float) -> float:
        """Safety margin between the local deadline and the earliest moment a
        peer can acquire: room for ``lost`` callbacks to stop the work, for the
        watchdog to run on a busy loop, and for the second the database may cut
        off the stored expiry (DATETIME columns without fractional seconds).
        A quarter of the TTL, at most one heartbeat interval, at least
        ``min_margin_s`` (2 s) — 7.5 s with the default 30 s / 10 s."""
        return min(max(self._min_margin_s, min(float(self.heartbeat_s), ttl / 4.0)), ttl / 2.0)

    def want(self, name: str, *, acquired: Callback = None, lost: Callback = None, ttl: Optional[float] = None) -> None:
        """Register interest in ``name``; ``acquired``/``lost`` fire on transitions.

        If the lease is already held when a new callback registers, ``acquired``
        fires right away (a plugin that starts late still gets its signal).
        ``acquired`` receives the fencing token when it declares a ``token``
        parameter.
        """
        entry = self._wanted.get(name)
        if entry is None:
            entry = self._wanted[name] = _Wanted(ttl=self._effective_ttl(name, ttl))
        if acquired is not None or lost is not None:
            entry.callbacks.append((acquired, lost))
            if self.holds(name):
                self._fire(acquired, name, "acquired", self._held[name].token)
        self.kick()

    def _effective_ttl(self, name: str, ttl: Optional[float]) -> float:
        """A lease must survive one missed renewal: ttl >= 2 × heartbeat."""
        value = float(ttl or self.ttl)
        floor = 2.0 * float(self.heartbeat_s)
        if value < floor:
            log.warning(
                "LEASE: ttl %.0fs requested for '%s' is below two heartbeat intervals (%.0fs) — using %.0fs; "
                "a shorter lease expires between two renewals",
                value, name, floor, floor,
            )
            return floor
        return value

    def forget(self, name: str, *, acquired: Callback = None, lost: Callback = None,
               fire_lost: bool = False) -> None:
        """Drop a callback pair (or every interest in ``name`` when none given).

        When the last interest goes, a held lease is released so a peer can
        take over immediately instead of after the TTL. By default the ``lost``
        callback is **not** fired for the removed pair — the owner is tearing
        down. ``fire_lost=True`` (``ctx.release_lease``) fires it when the lease
        was held, so the owner's single "stop the work" path runs; the lease is
        released once those callbacks returned.
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
        pending: list = []
        if fire_lost and was_held:
            for _acquired, lost_cb in removed:
                task = self._fire(lost_cb, name, "lost")
                if task is not None:
                    pending.append(task)
        if not entry.callbacks:
            self._wanted.pop(name, None)
            if was_held:
                self._held.pop(name, None)
                self._transition(name)
                self._release_in_background(name, pending)
                self._notify(name, False)

    def _release_in_background(self, name: str, pending: Optional[list] = None) -> None:
        coro = self._release_and_announce(name, pending or [])
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
        """Give a held lease up explicitly (admin action, drain).

        Order: ``lost`` callbacks first (async ones awaited for at most
        ``LOST_CALLBACK_TIMEOUT_S``), then the release at the coordinator, then
        the broadcast that makes a peer acquire on the spot. The lease stays
        wanted — this instance contends again on its next tick.
        """
        if name not in self._held:
            return False
        await self._hand_over([name])
        return True

    async def _hand_over(self, names: list[str]) -> None:
        pending: list = []
        handed: list[str] = []
        for name in names:
            if self._held.pop(name, None) is None:
                continue
            handed.append(name)
            self._transition(name)
            pending.extend(self._fire_all(name, "lost"))
            self._notify(name, False)
        # One bounded wait for all of them, then the releases side by side: a
        # drain hands five leases over in the time it takes to hand over one.
        await self._settle(pending)
        await asyncio.gather(*(self._release_and_announce(name, []) for name in handed))

    async def _settle(self, pending: list) -> None:
        """Wait (bounded) for async ``lost`` callbacks so the work has stopped
        before a peer is allowed to start it."""
        if not pending:
            return
        _done, late = await asyncio.wait(pending, timeout=self.LOST_CALLBACK_TIMEOUT_S)
        if late:
            log.warning(
                "LEASE: %s lost callback(s) still running after %.0fs — releasing anyway",
                len(late), self.LOST_CALLBACK_TIMEOUT_S,
            )

    def kick(self) -> None:
        """Run the acquire pass as soon as possible (a peer released a lease)."""
        if self._kick is not None:
            self._kick.set()

    async def start(self) -> None:
        if self._running:
            return
        self._running, self._stopped = True, False
        self._kick = asyncio.Event()
        self._deadlines_changed = asyncio.Event()
        self._task = bus.create_tracked_task(self._loop(), name="cluster:lease_keeper")
        self._watchdog_task = bus.create_tracked_task(self._watchdog(), name="cluster:lease_watchdog")

    async def stop(self) -> None:
        """Stop contending and hand every held lease over (``lost`` first)."""
        self._running, self._stopped = False, True
        for task in (self._task, self._watchdog_task):
            if task is not None and not task.done():
                task.cancel()
        self._task = self._watchdog_task = None
        for attempts in self._inflight.values():
            for attempt in attempts:
                attempt.cancel()
        self._inflight.clear()
        await self._hand_over(list(self._held))

    async def tick(self) -> None:
        """One acquire/renew pass over every wanted lease (also used by tests).

        Returns when every attempt of this round answered, at the latest after
        one heartbeat interval — attempts that are still pending then keep
        running and apply their result when it arrives (if it still counts).
        """
        async with self._lock():
            self.fence_expired()
            attempts = [task for task in (self._start_attempt(name) for name in list(self._wanted)) if task is not None]
            if attempts:
                await asyncio.wait(attempts, timeout=self._round_timeout())
            self.fence_expired()

    def fence_expired(self) -> list[str]:
        """Declare every holding lost whose self-fencing deadline has passed.

        Called by the watchdog at the deadline and by every tick before it
        renews anything; returns the names that were fenced.
        """
        now = self._clock()
        fenced: list[str] = []
        if not self._fencing:
            return fenced
        for name, holding in list(self._held.items()):
            if now < self._valid_until(holding):
                continue
            log.error(
                "LEASE: '%s' was not renewed for %.1fs (ttl %.0fs) — treating it as lost before a peer can take it",
                name, now - holding.since, holding.ttl,
            )
            self._lose(name)
            fenced.append(name)
        return fenced

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._tick_lock is None or self._tick_lock[0] is not loop:
            self._tick_lock = (loop, asyncio.Lock())
        return self._tick_lock[1]

    def _valid_until(self, holding: _Holding) -> float:
        return holding.since + holding.ttl - self.margin(holding.ttl)

    def _round_timeout(self) -> float:
        timeout = float(self.heartbeat_s)
        if self._held and self._fencing:
            # No point in holding the round open past the next deadline — the
            # fencing right after the wait must not be late because of it.
            earliest = min(self._valid_until(h) for h in self._held.values())
            timeout = min(timeout, max(0.0, earliest - self._clock()) + 0.01)
        return timeout

    def _transition(self, name: str) -> None:
        self._generation[name] = self._generation.get(name, 0) + 1
        if self._deadlines_changed is not None:
            self._deadlines_changed.set()

    def _start_attempt(self, name: str) -> Optional[asyncio.Task]:
        entry = self._wanted.get(name)
        if entry is None or self._stopped:
            return None
        running = self._inflight.setdefault(name, set())
        if len(running) >= self.MAX_INFLIGHT:
            return None  # the coordinator is sitting on earlier calls — do not stack more threads
        if running:
            # The previous round's call has not answered within a whole
            # interval: its connection is probably dead. Let this one (and the
            # heartbeat) start on fresh connections.
            self._refresh_connections(f"lease call for '{name}' still pending")
        task = bus.create_tracked_task(
            self._attempt(name, entry.ttl, self._generation.get(name, 0), fresh=name not in self._held),
            name=f"cluster:lease_attempt:{name}",
        )
        running.add(task)
        task.add_done_callback(lambda t, n=name: self._inflight.get(n, set()).discard(t))
        return task

    def _refresh_connections(self, reason: str) -> None:
        refresh = getattr(self._coordinator, "refresh_connections", None)
        if callable(refresh):
            try:
                refresh(reason)
            except Exception:
                log.debug("LEASE: connection refresh failed", exc_info=True)

    async def _attempt(self, name: str, ttl: float, generation: int, *, fresh: bool) -> None:
        started = self._clock()
        granted: Optional[bool]
        token: Optional[int] = None
        try:
            granted, token = await self._call_acquire(name, ttl, fresh)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            granted = None
            log.debug("LEASE: acquire/renew of '%s' failed: %s", name, exc)
        self._apply(name, ttl, generation, started, granted, token)

    async def _call_acquire(self, name: str, ttl: float, fresh: bool) -> tuple[Optional[bool], Optional[int]]:
        """(granted, token) from the coordinator. Drivers with fencing tokens
        implement ``acquire``; plain ``try_acquire`` drivers yield no token."""
        acquire = getattr(self._coordinator, "acquire", None)
        if callable(acquire):
            token = await acquire(name, ttl, fresh=fresh)
            return token is not None, token
        answer = await self._coordinator.try_acquire(name, ttl)
        return (answer if isinstance(answer, bool) else None), None

    def _apply(self, name: str, ttl: float, generation: int, started: float,
               granted: Optional[bool], token: Optional[int]) -> None:
        """Fold one attempt's answer into the local state — if it still counts."""
        if self._generation.get(name, 0) != generation:
            # Something happened to this lease while the call was under way
            # (fenced, forgotten, released, acquired by a parallel attempt):
            # the answer describes a past that no longer exists.
            if granted and name not in self._wanted and name not in self._held:
                # Nobody here wants it any more, yet the late call took it at
                # the coordinator — give it back instead of blocking the peers
                # for a full TTL.
                self._release_in_background(name)
            return
        holding = self._held.get(name)
        if granted and self._fencing and started + ttl - self.margin(ttl) <= self._clock():
            granted = None  # answered so late that the grant has (almost) expired already
        if granted:
            if holding is not None:
                if token is None or holding.token is None or token == holding.token:
                    holding.since = max(holding.since, started)   # plain renewal
                    if self._deadlines_changed is not None:
                        self._deadlines_changed.set()
                    return
                # Renewed, but under a new token: the coordinator did not see
                # an uninterrupted holding (row force-released, or expired and
                # re-taken). Whatever ran under the old token must stop; the
                # next tick acquires afresh (one more token) and fires
                # ``acquired`` — never both callbacks in the same breath.
                log.warning("LEASE: '%s' came back under a new token — the holding was interrupted", name)
                self._lose(name)
                return
            if name not in self._wanted:
                self._release_in_background(name)
                return
            self._held[name] = _Holding(since=started, ttl=ttl, token=token)
            self._transition(name)
            log.info("LEASE: acquired '%s'%s", name, f" (token {token})" if token is not None else "")
            self._fire_all(name, "acquired", token)
            self._notify(name, True)
        elif granted is False:
            if holding is not None:
                log.warning("LEASE: lost '%s' to a peer", name)
                self._lose(name)
        # granted is None → coordinator unreachable/late: the deadline decides.

    def _lose(self, name: str) -> None:
        if self._held.pop(name, None) is None:
            return
        self._transition(name)
        self._fire_all(name, "lost")
        self._notify(name, False)

    async def _loop(self) -> None:
        while self._running:
            # Clear BEFORE the tick: a kick that arrives while the tick runs
            # (a peer released a lease) must trigger the next pass, not vanish.
            self._kick.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("LEASE: keeper tick failed", exc_info=True)
            # Jitter keeps N instances from hammering the DB in lock-step.
            delay = self.heartbeat_s * random.uniform(0.8, 1.0)
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _watchdog(self) -> None:
        """Fire ``lost`` at the earliest deadline, whatever the tick is doing."""
        while self._running:
            self._deadlines_changed.clear()
            timeout: Optional[float] = None
            if self._held and self._fencing:
                earliest = min(self._valid_until(h) for h in self._held.values())
                timeout = max(0.0, earliest - self._clock())
            try:
                await asyncio.wait_for(self._deadlines_changed.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    self.fence_expired()
                except Exception:
                    log.error("LEASE: fencing failed", exc_info=True)

    async def _release_quietly(self, name: str) -> None:
        try:
            # Bounded: a release that hangs must not hold a drain up — the
            # lease expires on its own after the TTL.
            await asyncio.wait_for(self._coordinator.release(name), timeout=max(1.0, float(self.heartbeat_s)))
        except Exception as exc:
            log.debug("LEASE: quiet release of '%s' failed: %s", name, exc)

    async def _release_and_announce(self, name: str, pending: list) -> None:
        """Release at the coordinator (after the holder's ``lost`` callbacks),
        then tell the peers so one of them acquires the lease on the spot
        instead of on its next heartbeat."""
        await self._settle(pending)
        await self._release_quietly(name)
        try:
            bus.emit(RELEASED_TOPIC, {"name": name}, cluster=True)
        except Exception:
            log.debug("LEASE: release broadcast failed", exc_info=True)

    def _fire_all(self, name: str, event: str, token: Optional[int] = None) -> list:
        """Run every callback of ``name``; returns the tasks of async ones."""
        entry = self._wanted.get(name)
        if entry is None:
            return []
        tasks = []
        for acquired, lost in list(entry.callbacks):
            task = self._fire(acquired if event == "acquired" else lost, name, event, token)
            if task is not None:
                tasks.append(task)
        return tasks

    @staticmethod
    def _fire(callback: Callback, name: str, event: str, token: Optional[int] = None) -> Optional[asyncio.Task]:
        if callback is None:
            return None
        try:
            how = _accepts_token(callback) if event == "acquired" else None
            if how == "keyword":
                result = callback(token=token)
            elif how == "positional":
                result = callback(token)
            else:
                result = callback()
            if inspect.isawaitable(result):
                return bus.create_tracked_task(result, name=f"cluster:lease_{event}:{name}")
        except Exception:
            log.error("LEASE: %s callback for '%s' raised", event, name, exc_info=True)
        return None

    def _notify(self, name: str, held: bool) -> None:
        if self.on_change is not None:
            try:
                self.on_change(name, held)
            except Exception:
                log.debug("LEASE: on_change hook failed", exc_info=True)
