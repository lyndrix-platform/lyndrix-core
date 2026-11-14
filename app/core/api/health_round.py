"""Plugin health round for ``GET /api/health`` — cached and single-flight.

Every dashboard polls ``/api/health``. The round (one ``health()`` call per
active plugin) is cached for a short window; when the window expires exactly ONE
caller refreshes it while the others wait for that result. Before, every poller
that arrived after expiry started its own full round concurrently (N pollers ×
all plugins' ``health()`` — a stampede against plugins and their databases).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

from core.api import PluginHealthStatus

CACHE_TTL_SECONDS = 10.0
HEALTH_TIMEOUT_SECONDS = 5.0
_SEVERITY = {"error": 3, "degraded": 2, "unknown": 1, "ok": 0}


def skip_reason(runtime: Optional[dict]) -> Optional[str]:
    """Why a plugin's ``health()`` is not called right now (``None`` = call it).

    * ``starting`` — the warm-up window: ``health()`` would only add load to the
      very work the plugin is waiting for, and "starting" is not an error.
    * blocking ``maintenance`` — the plugin announced it is unavailable (its
      routes answer 503); probing it would report a planned window as an error.
      A non-blocking maintenance banner leaves the plugin fully operational, so
      it is still probed.
    """
    if not runtime:
        return None
    phase = runtime.get("phase")
    if phase == "starting":
        return "starting"
    if phase == "maintenance" and runtime.get("blocking"):
        return "maintenance"
    return None


class PluginHealthRound:
    def __init__(self) -> None:
        self._at = 0.0
        self._worst = "ok"
        self._plugins: Dict[str, Any] = {}
        self._lock: Optional[asyncio.Lock] = None
        # health() calls that outlived their timeout, per plugin. A call that
        # hangs in a worker thread (NFS, a dead socket) cannot be cancelled;
        # starting another one every round would leak one executor thread per
        # poll window until the default executor is exhausted.
        self._still_running: Dict[str, asyncio.Task] = {}

    async def get(self, module_manager) -> Tuple[str, Dict[str, Any], bool]:
        """Return ``(worst_status, per_plugin, served_from_cache)``."""
        if self._fresh():
            return self._worst, self._plugins, True
        if self._lock is None:
            self._lock = asyncio.Lock()  # created lazily: needs the running loop
        async with self._lock:
            if self._fresh():
                # Another caller refreshed while we waited for the lock.
                return self._worst, self._plugins, True
            await self._refresh(module_manager)
            return self._worst, self._plugins, False

    def _fresh(self) -> bool:
        return time.monotonic() - self._at < CACHE_TTL_SECONDS

    async def _refresh(self, module_manager) -> None:
        entries = [
            (mid, entry)
            for mid, entry in list(module_manager.registry.items())
            if entry.get("manifest") and entry["manifest"].type == "PLUGIN"
            and entry.get("status") == "active"
        ]
        gathered = await asyncio.gather(*(self._call_health(mid, entry) for mid, entry in entries))
        plugins: Dict[str, Any] = {}
        worst = "ok"
        for module_id, health_status in gathered:
            plugins[module_id] = {
                **health_status.model_dump(),
                "runtime": module_manager.get_runtime(module_id),
            }
            if _SEVERITY.get(health_status.status, 0) > _SEVERITY.get(worst, 0):
                worst = health_status.status
        self._worst, self._plugins, self._at = worst, plugins, time.monotonic()

    async def _call_health(self, module_id: str, entry: dict) -> Tuple[str, PluginHealthStatus]:
        module = entry.get("module")
        ctx = entry.get("context")
        if module is None or ctx is None or not hasattr(module, "health"):
            return module_id, PluginHealthStatus(status="unknown")
        reason = skip_reason(getattr(ctx, "runtime", None))
        if reason:
            return module_id, PluginHealthStatus(status="unknown", details={"reason": reason})

        previous = self._still_running.get(module_id)
        if previous is not None:
            if not previous.done():
                # Never stack a second call on top of a hanging one.
                return module_id, PluginHealthStatus(
                    status="degraded", details={"reason": "health check still running"}
                )
            del self._still_running[module_id]

        try:
            t0 = time.monotonic()
            task = asyncio.ensure_future(module.health(ctx))
            done, _pending = await asyncio.wait({task}, timeout=HEALTH_TIMEOUT_SECONDS)
            if not done:
                # Not cancelled on purpose: cancelling the awaiting task would
                # not stop a blocked worker thread, it would only hide it and
                # let the next round start another. Remember it instead.
                task.add_done_callback(_consume_result)
                self._still_running[module_id] = task
                return module_id, PluginHealthStatus(status="error", details={"reason": "health() timed out"})
            result = task.result()
            elapsed_ms = (time.monotonic() - t0) * 1000
            if not isinstance(result, PluginHealthStatus):
                result = PluginHealthStatus(status="unknown", details={"raw": str(result)})
            result.latency_ms = round(elapsed_ms, 2)
            return module_id, result
        except Exception as exc:
            return module_id, PluginHealthStatus(status="error", details={"reason": str(exc)})


def _consume_result(task: asyncio.Task) -> None:
    """Retrieve a late health() outcome so asyncio does not log it as unhandled."""
    if not task.cancelled():
        task.exception()


plugin_health_round = PluginHealthRound()
