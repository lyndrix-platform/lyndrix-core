import asyncio
from typing import Optional
from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:MonitorService")

# Lazy import psutil to handle missing dependency gracefully
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False
    log.warning("MONITOR: psutil not installed. System metrics will be unavailable.")


class MonitorService:
    def __init__(self):
        self.stats = {'cpu': 0, 'ram': 0, 'disk': 0}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        bus.subscribe("system:started")(self.start_monitoring)

    async def start_monitoring(self, payload=None):
        if not _PSUTIL_AVAILABLE:
            log.warning("MONITOR: Skipping monitoring loop — psutil unavailable.")
            return

        if self._running:
            log.debug("MONITOR: Already running, skipping duplicate start.")
            return

        self._running = True
        log.info("MONITOR: Starting system metrics loop...")
        self._task = bus.create_tracked_task(
            self._monitor_loop(),
            name="monitor_service:metrics_loop"
        )

    async def _monitor_loop(self):
        """Resilient metrics loop — survives transient psutil errors."""
        consecutive_failures = 0
        max_failures = 10

        try:
            while self._running:
                try:
                    self.stats.update({
                        'cpu': psutil.cpu_percent(),
                        'ram': psutil.virtual_memory().percent,
                        'disk': psutil.disk_usage('/').percent
                    })
                    # Tag with the instance id: behind a load balancer every
                    # SSE client sees the numbers of whichever instance holds
                    # its stream — the shell shows which one.
                    try:
                        from core.components.cluster.logic.cluster_service import cluster_service

                        instance_id = cluster_service.instance_id
                    except Exception:  # pragma: no cover - defensive
                        instance_id = None
                    bus.emit("system:metrics_update", {**self.stats, "instance_id": instance_id})
                    consecutive_failures = 0
                except (OSError, psutil.Error) as e:
                    consecutive_failures += 1
                    log.warning(f"MONITOR: Metrics collection failed ({consecutive_failures}/{max_failures}): {e}")
                    if consecutive_failures >= max_failures:
                        log.error("MONITOR: Too many consecutive failures. Stopping metrics loop.")
                        break
                except Exception as e:
                    consecutive_failures += 1
                    log.error(f"MONITOR: Unexpected error in metrics loop: {e}", exc_info=True)
                    if consecutive_failures >= max_failures:
                        log.error("MONITOR: Fatal: stopping metrics loop after repeated failures.")
                        break

                await asyncio.sleep(2)
        except asyncio.CancelledError:
            log.info("MONITOR: Metrics loop cancelled gracefully.")
        finally:
            self._running = False

    async def stop(self):
        """Gracefully stops the monitoring loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("MONITOR: Stopped.")


monitor_service = MonitorService()