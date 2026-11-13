"""Heartbeat and lease renewal against a coordinator that hangs (core 1.11.1).

The 2026-11-11 incident: a pooled DB connection whose peer silently vanished
blocked a heartbeat for the kernel TCP timeout (~16 min) and every later beat
queued behind it. Both paths are now bounded by the lease TTL.
"""
import asyncio
import time

from core.components.cluster.logic.cluster_service import cluster_service
from core.components.cluster.logic.leases import LeaseKeeper


class HangingCoordinator:
    driver = "fake"
    instance_id = "me"

    def __init__(self, hang_s: float = 30.0):
        self.hang_s = hang_s
        self.calls = 0

    async def heartbeat(self, meta):
        self.calls += 1
        await asyncio.sleep(self.hang_s)

    async def try_acquire(self, name, ttl=None):
        self.calls += 1
        await asyncio.sleep(self.hang_s)
        return True

    async def instances(self):
        return []

    async def holder(self, name):
        return None

    async def release(self, name):
        return True


def test_heartbeat_is_abandoned_after_the_lease_ttl(monkeypatch):
    coord = HangingCoordinator()
    monkeypatch.setattr(cluster_service, "coordinator", coord)
    monkeypatch.setattr(cluster_service, "ttl", 1)
    monkeypatch.setattr(cluster_service, "heartbeat_s", 1)
    monkeypatch.setattr(cluster_service, "_hb_failures", 0)
    monkeypatch.setattr(cluster_service, "_hb_last_ok_mono", 0.0)
    assert cluster_service.heartbeat_timeout_s == 1.0

    started = time.monotonic()
    asyncio.run(cluster_service._heartbeat_once())
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"heartbeat waited {elapsed:.1f}s for a hung coordinator"
    assert cluster_service._hb_failures == 1
    assert "timed out" in (cluster_service.last_heartbeat_error or "")
    status = cluster_service.heartbeat_status()
    assert status["failures"] == 1 and status["timeout_s"] == 1.0 and status["last_duration_s"] >= 1.0
    assert cluster_service.snapshot()["coordinator"]["heartbeat"]["last_error"] == cluster_service.last_heartbeat_error


def test_heartbeat_recovery_resets_failure_counter(monkeypatch):
    class Healthy(HangingCoordinator):
        async def heartbeat(self, meta):
            self.calls += 1

    coord = Healthy()
    monkeypatch.setattr(cluster_service, "coordinator", coord)
    monkeypatch.setattr(cluster_service, "ttl", 1)
    monkeypatch.setattr(cluster_service, "heartbeat_s", 1)
    monkeypatch.setattr(cluster_service, "_hb_failures", 4)
    monkeypatch.setattr(cluster_service, "last_heartbeat_error", "timed out after 1s")

    asyncio.run(cluster_service._heartbeat_once())

    assert cluster_service._hb_failures == 0
    assert cluster_service.last_heartbeat_error is None
    assert cluster_service.last_heartbeat_ok_at is not None
    assert cluster_service.heartbeat_status()["last_ok_at"]


def test_hung_renewal_fences_the_lease_after_the_ttl():
    coord = HangingCoordinator(hang_s=0.0)
    keeper = LeaseKeeper(coord, ttl=1, heartbeat_s=1)
    events = []
    keeper.want("x", acquired=lambda: events.append("acq"), lost=lambda: events.append("lost"))

    async def run():
        await keeper.tick()
        assert events == ["acq"] and keeper.holds("x")
        coord.hang_s = 30.0  # the database stopped answering
        started = time.monotonic()
        await keeper.tick()  # try_acquire is abandoned after ttl → renewal missed → fenced
        assert time.monotonic() - started < 5
        assert events == ["acq", "lost"] and not keeper.holds("x")

    asyncio.run(run())
