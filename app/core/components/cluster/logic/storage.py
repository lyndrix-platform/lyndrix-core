"""StorageProbe — every STORAGE_DIR access of the cluster layer, one at a time.

``STORAGE_DIR`` is a network mount in real clusters (NFS / CIFS / Kubernetes
RWX volume). When that mount hangs, a plain ``open()`` or ``listdir()`` blocks
its thread for as long as the outage lasts — on a hard mount: forever. The
heartbeat used to write its marker file through ``asyncio.to_thread`` on every
beat, so each beat parked one more worker of the *shared* default executor
until none was left for database, Vault or login work.

The probe runs at most ONE storage job at a time on its own daemon thread:

* a job submitted while the previous one is still running is **skipped**
  (never queued) — so a hung mount costs exactly one blocked thread;
* the thread is a daemon: it cannot keep the process from exiting;
* how long the running job has been stuck is visible (:attr:`stuck_for_s`) for
  the preflight and ``GET /api/cluster``.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Optional

from core.logger import get_logger

log = get_logger("Core:Cluster:Storage")


class StorageBusy(RuntimeError):
    """The previous storage job has not returned yet (slow or hung mount)."""

    def __init__(self, stuck_for_s: float, label: Optional[str]) -> None:
        super().__init__(f"storage probe busy for {stuck_for_s:.0f}s ({label or 'io'})")
        self.stuck_for_s = stuck_for_s
        self.label = label


class StorageProbe:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight: Optional[Future] = None
        self._inflight_since = 0.0
        self._inflight_label: Optional[str] = None
        self.skipped = 0          # jobs not started because one was still running
        self.completed = 0

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        future = self._inflight
        return future is not None and not future.done()

    @property
    def stuck_for_s(self) -> float:
        """Seconds the running job has been in flight (0.0 when idle)."""
        return max(0.0, self._clock() - self._inflight_since) if self.busy else 0.0

    @property
    def stuck_label(self) -> Optional[str]:
        return self._inflight_label if self.busy else None

    # ------------------------------------------------------------------
    # running jobs
    # ------------------------------------------------------------------

    def submit(self, fn: Callable[..., Any], *args: Any, label: str = "io") -> Optional[Future]:
        """Start ``fn(*args)`` on the storage thread and return its future —
        or ``None`` when the previous job is still running (the job is dropped)."""
        with self._lock:
            if self.busy:
                self.skipped += 1
                return None
            future: Future = Future()
            # RUNNING from the start: nobody can cancel it under the worker,
            # which would make its set_result() raise.
            future.set_running_or_notify_cancel()
            self._inflight, self._inflight_since, self._inflight_label = future, self._clock(), label

        def _work() -> None:
            try:
                result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                future.set_exception(exc)
            else:
                future.set_result(result)
            self.completed += 1

        threading.Thread(target=_work, name=f"lyndrix-storage:{label}", daemon=True).start()
        return future

    async def call(self, fn: Callable[..., Any], *args: Any, label: str = "io", timeout: float = 5.0) -> Any:
        """Run ``fn`` on the storage thread and wait at most ``timeout`` seconds.

        Raises :class:`StorageBusy` when an earlier job still runs and
        ``asyncio.TimeoutError`` when this one does not finish in time (it keeps
        running; later calls are skipped until it returns).
        """
        future = self.submit(fn, *args, label=label)
        if future is None:
            raise StorageBusy(self.stuck_for_s, self._inflight_label)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)

    def status(self) -> dict:
        return {
            "busy": self.busy,
            "stuck_for_s": round(self.stuck_for_s, 1),
            "stuck_job": self.stuck_label,
            "skipped": self.skipped,
        }
