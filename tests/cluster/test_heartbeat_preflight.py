"""Heartbeat, storage probe and preflight are independent and bounded (CL-06, CL-09).

* the periodic preflight is not part of the heartbeat loop any more — a slow
  check cannot stop the beats;
* STORAGE_DIR is only ever touched by one thread — a hung mount cannot eat the
  shared executor;
* ``/api/ready`` follows the live heartbeat: one transient error during a
  preflight no longer pins it at 503, and a starved heartbeat no longer leaves
  it at 200.
"""
import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from core.components.cluster.logic import cluster_service as cs_mod
from core.components.cluster.logic import preflight
from core.components.cluster.logic.cluster_service import cluster_service
from core.components.cluster.logic.coordinator import InstanceInfo
from core.components.cluster.logic.drivers.db import DbCoordinator
from core.components.cluster.logic.preflight import run_checks
from core.components.cluster.logic.storage import StorageBusy, StorageProbe
from core.services import boot_service, db_instance, vault_instance


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _inst(iid, me=False, fp=None):
    return InstanceInfo(iid, "h", ["api", "leader"], "1.11.1", "1.13.0", _now(), _now(), 0.5, True, me,
                        {"secret_fp": fp or cluster_service.secret_fingerprint})


class Coord:
    """Healthy coordinator of a two-instance cluster; individual calls can be
    made to fail (``fail_instances``) or hang (``hang``)."""
    driver = "db"

    def __init__(self):
        self.instance_id = cluster_service.instance_id
        self.beats: list[float] = []
        self.fail_instances = 0
        self.hang: set = set()
        self.beat_takes = 0.0

    async def _maybe_hang(self, op):
        if op in self.hang:
            await asyncio.sleep(3600)

    async def heartbeat(self, meta):
        await self._maybe_hang("heartbeat")
        if self.beat_takes:
            await asyncio.sleep(self.beat_takes)
        self.beats.append(time.monotonic())

    async def instances(self):
        await self._maybe_hang("instances")
        if self.fail_instances:
            self.fail_instances -= 1
            raise ConnectionError("(2013, 'Lost connection to MySQL server during query')")
        return [_inst(self.instance_id, True), _inst("pod-2")]

    async def holder(self, name):
        return None

    async def leases(self):
        await self._maybe_hang("leases")
        return []

    async def server_info(self):
        await self._maybe_hang("server_info")
        return {"dialect": "mariadb", "version": "11.8"}

    async def db_now(self):
        await self._maybe_hang("db_now")
        return _now()


@pytest.fixture()
def svc(monkeypatch):
    """The cluster service singleton as a booted, ready cluster instance with a
    healthy fake coordinator and a fresh storage probe."""
    from core.components.boot.logic.boot_service import BootPhase

    coord = Coord()
    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)
    monkeypatch.setattr(cluster_service, "coordinator", coord)
    monkeypatch.setattr(cluster_service, "mode", "cluster")
    monkeypatch.setattr(cluster_service, "started", True)
    monkeypatch.setattr(cluster_service, "draining", False)
    monkeypatch.setattr(cluster_service, "ttl", 30)
    monkeypatch.setattr(cluster_service, "heartbeat_s", 10)
    monkeypatch.setattr(cluster_service, "preflight_results", [])
    monkeypatch.setattr(cluster_service, "preflight_at", None)
    monkeypatch.setattr(cluster_service, "_preflight_recheck", False)
    monkeypatch.setattr(cluster_service, "_preflight_quick_rechecks", 0)
    monkeypatch.setattr(cluster_service, "_preflight_task", None)
    monkeypatch.setattr(cluster_service, "_preflight_running", False)
    monkeypatch.setattr(cluster_service, "_instances", [])
    monkeypatch.setattr(cluster_service, "_hb_failures", 0)
    monkeypatch.setattr(cluster_service, "_hb_last_ok_mono", 0.0)
    monkeypatch.setattr(cluster_service, "_hb_last_ok_started", 0.0)
    monkeypatch.setattr(cluster_service, "last_heartbeat_error", None)
    monkeypatch.setattr(cluster_service, "_clock", time.monotonic)
    monkeypatch.setattr(cluster_service, "storage", StorageProbe(clock=lambda: cluster_service._clock()))
    monkeypatch.setattr(cluster_service, "_write_storage_marker", lambda: None)
    monkeypatch.setattr(cluster_service, "storage_markers", lambda: {"pod-2": 1.0})
    monkeypatch.setattr(preflight, "_pinned_roles", lambda: {})
    yield cluster_service


# --- storage probe ------------------------------------------------------------

def test_storage_probe_runs_one_job_at_a_time():
    clock = FakeClock()
    probe = StorageProbe(clock=clock)
    gate, entered = threading.Event(), threading.Event()

    def hung_io():
        entered.set()
        gate.wait(10)
        return "done"

    first = probe.submit(hung_io, label="marker")
    assert first is not None and entered.wait(2)
    clock.advance(42)
    assert probe.busy and probe.stuck_for_s == 42 and probe.stuck_label == "marker"
    assert probe.submit(lambda: 1, label="again") is None      # dropped, not queued
    assert probe.skipped == 1 and probe.status()["stuck_job"] == "marker"

    async def call_while_busy():
        with pytest.raises(StorageBusy) as err:
            await probe.call(lambda: 1, label="read", timeout=1)
        return err.value

    busy = asyncio.run(call_while_busy())
    assert busy.stuck_for_s == 42 and busy.label == "marker"

    gate.set()
    assert first.result(2) == "done"
    assert not probe.busy and probe.stuck_for_s == 0.0
    assert asyncio.run(probe.call(lambda: "next", timeout=2)) == "next"


def test_storage_probe_call_times_out_but_keeps_the_single_slot():
    probe = StorageProbe()
    gate = threading.Event()

    async def run():
        with pytest.raises(asyncio.TimeoutError):
            await probe.call(lambda: gate.wait(10), label="listdir", timeout=0.05)
        # the job is still on the mount → nothing else is sent after it
        assert probe.busy
        with pytest.raises(StorageBusy):
            await probe.call(lambda: 1, timeout=0.05)

    try:
        asyncio.run(run())
    finally:
        gate.set()


def test_storage_probe_hands_errors_to_the_caller():
    probe = StorageProbe()

    def boom():
        raise OSError("read-only file system")

    with pytest.raises(OSError):
        asyncio.run(probe.call(boom, timeout=2))
    assert not probe.busy


# --- CL-06: heartbeat vs. hung storage / slow preflight ---------------------------

def test_hung_storage_mount_blocks_one_thread_and_no_heartbeat(svc, monkeypatch):
    """p6 (b): the marker write on a hung hard mount used to park one worker of
    the shared executor per beat until asyncio.to_thread() stopped working."""
    gate = threading.Event()
    stuck = []

    def hung_marker_write():
        stuck.append(threading.current_thread().name)
        gate.wait(20)

    monkeypatch.setattr(svc, "_write_storage_marker", hung_marker_write)

    async def run():
        started = time.monotonic()
        for _ in range(8):
            await svc._heartbeat_once()
        elapsed = time.monotonic() - started
        other_work = await asyncio.wait_for(asyncio.to_thread(lambda: "served"), 2)
        return elapsed, other_work

    try:
        elapsed, other_work = asyncio.run(run())
    finally:
        gate.set()
    assert len(stuck) == 1, f"{len(stuck)} threads went after the hung mount"
    assert stuck[0].startswith("lyndrix-storage")           # not a worker of the shared executor
    assert svc.storage.skipped == 7
    assert len(svc.coordinator.beats) == 8 and svc._hb_failures == 0
    assert elapsed < 5 and other_work == "served"
    assert svc._meta()["storage_stuck_s"] >= 0
    assert svc.snapshot()["storage"]["skipped"] == 7


def test_slow_preflight_does_not_stop_the_heartbeat(svc, monkeypatch):
    """p6 (a): the periodic preflight ran inline in the heartbeat loop; a hung
    check meant no heartbeat until it returned."""
    monkeypatch.setattr(svc, "heartbeat_s", 0.05)
    monkeypatch.setattr(svc, "ttl", 1)
    monkeypatch.setattr(svc, "preflight_at", "booted")
    release = asyncio.Event
    state = {}

    async def hung_preflight():
        state["started"] = True
        await state["release"].wait()
        return []

    monkeypatch.setattr(svc, "run_preflight", hung_preflight)

    async def run():
        state["release"] = release()
        beats = asyncio.create_task(svc._heartbeat_loop())
        checks = svc._preflight_task = asyncio.create_task(svc._preflight_loop())
        await asyncio.sleep(0.05)
        svc.request_preflight()                      # e.g. a membership change
        await asyncio.sleep(0.8)
        hung = state.get("started") and svc._preflight_running
        state["release"].set()
        for task in (beats, checks):
            task.cancel()
        await asyncio.gather(beats, checks, return_exceptions=True)
        return hung

    assert asyncio.run(run()), "the preflight was expected to be hanging during the measurement"
    beats = svc.coordinator.beats
    assert len(beats) >= 5, f"only {len(beats)} heartbeat(s) while the preflight hung"
    assert max(b - a for a, b in zip(beats, beats[1:])) < 0.6   # far below the 1 s TTL


def test_heartbeat_runs_at_a_fixed_rate(svc, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(svc, "_clock", clock)
    started = clock()
    clock.advance(3)                       # a normal beat that took 3 s
    assert svc._next_beat_delay(started) == 7
    clock.advance(20)                      # a beat slower than the interval
    assert svc._next_beat_delay(started) == 0.0   # the next one is due at once


# --- readiness follows the live heartbeat ------------------------------------------

def test_readiness_turns_503_when_the_heartbeat_is_starved(svc, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(svc, "_clock", clock)

    assert svc.readiness() == (False, ["heartbeat"])           # no beat has succeeded yet
    asyncio.run(svc._heartbeat_once())
    assert svc.readiness() == (True, [])
    clock.advance(svc.ttl - 1)
    assert svc.readiness() == (True, [])
    clock.advance(2)                                             # > TTL without a good beat
    assert svc.readiness() == (False, ["heartbeat"])
    assert svc.heartbeat_status()["stale"] is True
    # The row a beat writes never says "not ready: heartbeat" — if it lands, the heartbeat works.
    assert svc._meta()["ready"] is True and svc._meta()["reasons"] == []
    asyncio.run(svc._heartbeat_once())
    assert svc.readiness() == (True, [])


def test_heartbeat_staleness_is_measured_from_the_start_of_the_last_good_beat(svc, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(svc, "_clock", clock)

    async def slow_but_successful(meta):
        clock.advance(20)                  # the row was written at the start; the answer came late

    monkeypatch.setattr(svc.coordinator, "heartbeat", slow_but_successful)
    asyncio.run(svc._heartbeat_once())
    clock.advance(11)                      # 31 s since the write, 11 s since the answer
    assert svc.readiness() == (False, ["heartbeat"])


def test_single_mode_has_no_heartbeat_reason(svc, monkeypatch):
    monkeypatch.setattr(svc, "mode", "single")
    assert svc.readiness() == (True, [])
    assert svc.heartbeat_status()["stale"] is False


def test_a_hung_heartbeat_makes_the_instance_unready_and_refreshes_the_pool(svc, monkeypatch):
    refreshed = []
    monkeypatch.setattr(svc, "ttl", 1)
    monkeypatch.setattr(svc, "heartbeat_s", 1)
    monkeypatch.setattr(svc.coordinator, "refresh_connections", lambda reason="": refreshed.append(reason), raising=False)

    async def run():
        await svc._heartbeat_once()                  # healthy
        svc.coordinator.hang.add("heartbeat")
        await svc._heartbeat_once()                  # abandoned after the 1 s TTL

    asyncio.run(run())
    assert svc._hb_failures == 1
    assert svc.readiness() == (False, ["heartbeat"])
    assert refreshed == ["heartbeat timed out"]


# --- CL-09: a transient error must not stick ---------------------------------------

def test_transient_error_during_the_preflight_does_not_pin_readiness(svc):
    """p7: one 2013 while the preflight refreshed the instance list cached
    'coordinator = fail' and kept /api/ready at 503 for up to five minutes."""
    async def run():
        await svc._heartbeat_once()
        await svc.run_preflight()
        healthy = svc.readiness()
        svc.coordinator.fail_instances = 1                 # VIP move: exactly one query dies
        await svc.run_preflight()
        during = svc.readiness()
        check = {c["id"]: c for c in svc.preflight_results}["coordinator"]
        recheck_wanted = svc._preflight_recheck
        # The next good beat asks for a second look instead of waiting 5 minutes.
        await svc._heartbeat_once()
        await asyncio.sleep(0.05)
        return healthy, during, check, recheck_wanted

    healthy, during, check, recheck_wanted = asyncio.run(run())
    assert healthy == (True, [])
    assert during == (True, []), "a failed refresh inside the preflight must not make the instance unready"
    assert check["status"] == "warn" and "last known view" in check["message"]
    assert recheck_wanted is True
    after = {c["id"]: c for c in svc.preflight_results}["coordinator"]
    assert after["status"] == "ok" and svc._preflight_recheck is False


def test_failed_refresh_keeps_the_last_known_view_for_the_other_checks(svc):
    async def run():
        await svc.run_preflight()                    # fills the view: 2 live instances
        svc.coordinator.fail_instances = 1
        return {c.id: c for c in await run_checks(svc)}

    checks = asyncio.run(run())
    assert checks["coordinator"].status == "warn" and checks["coordinator"].recheck
    assert checks["redundancy"].status == "ok"       # not "only this instance is alive"
    assert checks["roles"].status == "ok"


def test_hanging_checks_time_out_as_warnings(svc, monkeypatch):
    monkeypatch.setattr(preflight, "CHECK_TIMEOUT_S", 0.05)
    svc.coordinator.hang.update({"instances", "server_info", "db_now"})

    async def run():
        started = time.monotonic()
        checks = await run_checks(svc)
        return {c.id: c for c in checks}, time.monotonic() - started

    checks, elapsed = asyncio.run(run())
    assert elapsed < 3, f"preflight waited {elapsed:.1f}s for hanging checks"
    assert checks["coordinator"].status == "warn" and "no answer within" in checks["coordinator"].message
    assert checks["database"].status == "warn" and checks["database"].recheck
    assert checks["clock"].status == "skip" and checks["clock"].recheck
    assert not [c for c in checks.values() if c.status == "fail"]
    assert "recheck" not in checks["database"].to_dict()     # internal flag, not part of the API


def test_stuck_storage_probe_is_a_warning_not_a_readiness_failure(svc, monkeypatch):
    async def busy(timeout=0):
        raise StorageBusy(95.0, "marker")

    monkeypatch.setattr(svc, "read_storage_markers", busy)

    async def run():
        await svc._heartbeat_once()
        await svc.run_preflight()

    asyncio.run(run())
    check = {c["id"]: c for c in svc.preflight_results}["storage"]
    assert check["status"] == "warn" and "not responding" in check["message"] and "95" in check["message"]
    assert svc._preflight_recheck is True
    assert svc.readiness() == (True, [])


def test_undecided_checks_are_rechecked_soon_but_not_forever(svc):
    assert svc._preflight_delay() == cs_mod._PREFLIGHT_REFRESH_S
    svc._preflight_recheck = True
    assert svc._preflight_delay() == cs_mod._PREFLIGHT_RECHECK_BEATS * svc.heartbeat_s
    svc._preflight_quick_rechecks = cs_mod._PREFLIGHT_MAX_QUICK_RECHECKS
    assert svc._preflight_delay() == cs_mod._PREFLIGHT_REFRESH_S


def test_cluster_status_is_bounded_when_the_coordinator_hangs(svc, monkeypatch):
    monkeypatch.setattr(cs_mod, "_IO_TIMEOUT_S", 0.05)
    svc.coordinator.hang.update({"instances", "leases", "server_info"})

    async def run():
        started = time.monotonic()
        data = await svc.describe()
        return data, time.monotonic() - started

    data, elapsed = asyncio.run(run())
    assert elapsed < 3
    assert data["lease_table"] == [] and "did not answer" in data["coordinator"]["error"]
    assert "did not answer" in data["coordinator"]["server"]["error"]


# --- pool refresh hook of the db driver ---------------------------------------------

def test_db_driver_refreshes_the_pool_once_per_ttl_and_never_for_sqlite(tmp_path):
    from cluster_harness import make_engine

    sqlite_coord = DbCoordinator(instance_id="a", ttl=30, heartbeat_s=10, poll_ms=500,
                                 engine_getter=lambda: make_engine(tmp_path))
    assert sqlite_coord.refresh_connections("test") is False

    class FakeEngine:
        class dialect:
            name = "mariadb"

        def __init__(self):
            self.disposed = []

        def dispose(self, close=True):
            self.disposed.append(close)

    engine = FakeEngine()
    coord = DbCoordinator(instance_id="a", ttl=30, heartbeat_s=10, poll_ms=500, engine_getter=lambda: engine)
    assert coord.refresh_connections("heartbeat timed out") is True
    assert coord.refresh_connections("again") is False          # rate-limited
    assert engine.disposed == [False]                           # de-referenced, never closed under a caller
    assert coord.pool_refreshes == 1
