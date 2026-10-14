"""Graceful drain on SIGTERM (docs/ha/docker/rolling-updates.md).

Without a drain, ``docker stop`` / a pod termination closes the listener at
once: the load balancer still sends requests for one or two health-check
intervals and users see 502s. With ``LYNDRIX_DRAIN_SECONDS > 0`` the sequence
becomes:

1. SIGTERM → ``/api/ready`` answers 503 (``cluster_service.draining``), the LB
   takes the instance out after its next check;
2. wait ``LYNDRIX_DRAIN_SECONDS``;
3. close every SSE stream with ``event: reconnect`` (clients reconnect through
   the LB and land on a live instance);
4. release held leases (the leader hands over now, not after the TTL);
5. forward the signal to uvicorn's own handler → normal shutdown.

A second SIGTERM during the drain is forwarded immediately (force).
"""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Callable, Optional

from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:Cluster:Drain")


class DrainController:
    def __init__(self, *, seconds: int, on_drain: Callable[[], "asyncio.Future | None"]) -> None:
        self.seconds = int(seconds)
        self._on_drain = on_drain
        self._previous = None
        self.installed = False
        self.draining = False

    def install(self) -> None:
        if self.installed or self.seconds <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
            self._previous = signal.getsignal(signal.SIGTERM)
            loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
            self.installed = True
            log.info("DRAIN: SIGTERM drains for %ss before shutdown", self.seconds)
        except (NotImplementedError, RuntimeError, ValueError) as exc:  # Windows / non-main thread
            log.warning("DRAIN: could not install SIGTERM handler (%s) — shutting down immediately on stop", exc)

    def _on_sigterm(self) -> None:
        if self.draining:
            log.warning("DRAIN: second SIGTERM — forcing shutdown")
            self._forward()
            return
        self.draining = True
        log.warning("DRAIN: SIGTERM received — /api/ready → 503, draining for %ss", self.seconds)
        bus.emit("system:draining", {"seconds": self.seconds})
        bus.create_tracked_task(self._drain(), name="cluster:drain")

    async def _drain(self) -> None:
        try:
            await asyncio.sleep(self.seconds)
            try:
                from core.api.events_api import close_all_streams

                close_all_streams("reconnect")
            except Exception:
                log.debug("DRAIN: closing SSE streams failed", exc_info=True)
            try:
                result = self._on_drain()
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await asyncio.wait_for(result, timeout=10)
            except Exception:
                log.debug("DRAIN: on_drain hook failed", exc_info=True)
        finally:
            self._forward()

    def _forward(self) -> None:
        """Hand the signal to whoever owned SIGTERM before us (uvicorn)."""
        previous = self._previous
        try:
            loop = asyncio.get_running_loop()
            loop.remove_signal_handler(signal.SIGTERM)
        except Exception:
            pass
        if callable(previous):
            try:
                previous(signal.SIGTERM, None)
                return
            except Exception:
                log.debug("DRAIN: previous handler raised", exc_info=True)
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        except Exception:
            pass
        os.kill(os.getpid(), signal.SIGTERM)
