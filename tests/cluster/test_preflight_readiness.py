"""Preflight checks and /api/ready logic."""
import asyncio
from datetime import datetime, timezone

import pytest

from config import settings
from core.components.cluster.logic import preflight
from core.components.cluster.logic.cluster_service import cluster_service
from core.components.cluster.logic.coordinator import InstanceInfo
from core.components.cluster.logic.preflight import run_checks
from core.services import boot_service, db_instance, vault_instance


def _by_id(checks):
    return {c.id: c for c in checks}


def test_single_mode_preflight_has_no_failures():
    checks = _by_id(asyncio.run(run_checks(cluster_service)))
    assert checks["coordinator"].status == "skip"
    assert checks["redundancy"].status == "skip"
    assert checks["storage_secret"].status == "skip"
    assert checks["vault_nodes"].status == "skip"
    assert checks["roles"].status == "ok"
    assert checks["drain"].status == "ok"
    assert checks["storage"].status == "skip"
    assert not [c for c in checks.values() if c.status == "fail"]
    assert all(c.to_dict()["message"] for c in checks.values())


class _FakeCoordinator:
    async def server_info(self):
        return {"driver": "db", "dialect": "mysql", "version": "12.2.2-MariaDB", "wsrep_on": True, "snapshot_isolation": True}

    async def db_now(self):
        return datetime.now(timezone.utc).replace(tzinfo=None)


class _FakeClusterService:
    is_cluster = True
    started = True
    start_error = None
    roles = ["api"]
    drain_seconds = 0
    heartbeat_s = 10
    saw_forwarded_for = True
    coordinator = _FakeCoordinator()

    def __init__(self, instances, markers=None):
        self._instances = instances
        self._markers = markers if markers is not None else {}

    async def refresh_instances(self):
        return self._instances

    def storage_markers(self):
        return dict(self._markers)


def _instance(iid, roles, secret_fp, plugins_fp="p1", version="1.4.0", me="a", started_at=None):
    return InstanceInfo(
        instance_id=iid, hostname=iid, roles=roles, core_version=version, api_version="1.7.0",
        started_at=started_at, heartbeat_at=None, age_s=1.0, alive=True, is_self=iid == me,
        meta={"secret_fp": secret_fp, "plugins_fp": plugins_fp},
    )


def test_cluster_preflight_flags_contract_violations(monkeypatch):
    monkeypatch.setattr(preflight, "_pinned_roles", lambda: {"runner": ["lyndrix.plugin.iac_orchestrator"]})
    monkeypatch.setattr(settings, "LYNDRIX_UI_ENGINE", "react,nicegui")
    monkeypatch.setattr(settings, "LYNDRIX_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setattr(settings, "DB_ALLOW_DIRECT_NODE", False)
    monkeypatch.setattr(settings, "VAULT_NODES", None)
    svc = _FakeClusterService([
        _instance("a", ["api"], "fp-A", plugins_fp="p1"),
        _instance("b", ["api"], "fp-B", plugins_fp="p2", version="1.3.0"),
    ])
    checks = _by_id(asyncio.run(run_checks(svc)))
    assert checks["coordinator"].status == "ok"
    assert checks["redundancy"].status == "ok"
    # 1:1 tie without start times → the lowest id ("a", this instance) is the
    # reference: the mismatch is reported, but only "b" is unfit for traffic.
    assert checks["storage_secret"].status == "warn" and "b" in checks["storage_secret"].message
    assert checks["versions"].status == "warn"
    assert checks["plugin_artifacts"].status == "warn"
    assert checks["roles"].status == "fail" and "runner" in checks["roles"].message
    assert checks["ui_engine"].status == "warn"
    assert checks["trusted_proxies"].status == "warn"
    assert checks["database"].status == "warn" and "Galera" in checks["database"].message
    assert checks["clock"].status == "ok"
    assert checks["drain"].status == "warn"
    assert checks["storage"].status == "warn" and "NOT shared" in checks["storage"].message  # no marker of b


def test_cluster_preflight_healthy_pair(monkeypatch):
    monkeypatch.setattr(preflight, "_pinned_roles", lambda: {"runner": ["lyndrix.plugin.iac_orchestrator"]})
    monkeypatch.setattr(settings, "LYNDRIX_UI_ENGINE", "react")
    monkeypatch.setattr(settings, "LYNDRIX_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setattr(settings, "DB_ALLOW_DIRECT_NODE", True)
    svc = _FakeClusterService(
        [_instance("a", ["api", "leader"], "same"), _instance("b", ["api", "runner"], "same")],
        markers={"a": 2.0, "b": 7.5},   # both instances write into the same directory
    )
    svc.drain_seconds = 10
    checks = _by_id(asyncio.run(run_checks(svc)))
    assert not [c for c in checks.values() if c.status == "fail"]
    assert checks["roles"].status == "ok" and checks["storage_secret"].status == "ok"
    assert checks["trusted_proxies"].status == "ok" and checks["drain"].status == "ok"
    assert checks["database"].status == "ok"
    assert checks["storage"].status == "ok" and "shared mount" in checks["storage"].message


def _secret_check(instances):
    return _by_id(asyncio.run(run_checks(_FakeClusterService(instances))))["storage_secret"]


def test_secret_mismatch_fails_only_on_the_odd_instance():
    """CL-01: one joiner with a different STORAGE_SECRET must not take every
    healthy instance out of the load balancer."""
    def view(me):
        pods = [_instance(f"pod-{n}", ["api", "leader"], "fp-good", me=me) for n in (1, 2, 3, 4)]
        return pods + [_instance("docker-auto", ["api"], "fp-typo", me=me)]

    healthy = _secret_check(view("pod-2"))
    assert healthy.status == "warn" and "docker-auto" in healthy.message
    assert "pod-" not in healthy.message                       # only the odd one is named
    odd = _secret_check(view("docker-auto"))
    assert odd.status == "fail" and "4 of 5" in odd.message


def test_secret_tie_fails_only_the_younger_instance():
    old, young = datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 1, 9, 0)

    def view(me):
        # ids chosen so the id order contradicts the age order
        return [_instance("zz-old", ["api", "leader"], "fp-1", me=me, started_at=old),
                _instance("aa-young", ["api", "leader"], "fp-2", me=me, started_at=young)]

    assert _secret_check(view("zz-old")).status == "warn"
    assert _secret_check(view("aa-young")).status == "fail"


def test_secret_check_trusts_the_own_fingerprint_over_a_stale_row():
    """The self row may still carry the fingerprint of the previous process."""
    svc = _FakeClusterService([
        _instance("a", ["api"], "fp-stale"), _instance("b", ["api"], "fp-good"), _instance("c", ["api"], "fp-good"),
    ])
    svc.secret_fingerprint = "fp-good"
    assert _by_id(asyncio.run(run_checks(svc)))["storage_secret"].status == "ok"


def test_secret_mismatch_keeps_healthy_instances_ready(monkeypatch):
    """p9 as a test: 4 healthy pods + 1 joiner with a typo'd secret → the healthy
    pod's /api/ready stays 200 (it used to flip to 503 on every instance)."""
    from core.components.boot.logic.boot_service import BootPhase

    good = cluster_service.secret_fingerprint
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def inst(iid, fp, me=False):
        return InstanceInfo(iid, "h", ["api", "leader"], "1.11.1", "1.13.0", now, now, 0.5, True, me,
                            {"secret_fp": fp})

    class Coord(_FakeCoordinator):
        driver, instance_id = "db", cluster_service.instance_id
        view: list = []

        async def heartbeat(self, meta):
            return None

        async def instances(self):
            return list(Coord.view)

        async def holder(self, name):
            return None

    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)
    monkeypatch.setattr(cluster_service, "coordinator", Coord())
    monkeypatch.setattr(cluster_service, "mode", "cluster")
    monkeypatch.setattr(cluster_service, "started", True)
    monkeypatch.setattr(cluster_service, "draining", False)
    monkeypatch.setattr(cluster_service, "preflight_results", [])
    monkeypatch.setattr(cluster_service, "preflight_at", None)
    monkeypatch.setattr(cluster_service, "_instances", [])
    monkeypatch.setattr(cluster_service, "_write_storage_marker", lambda: None)

    healthy = [inst(cluster_service.instance_id, good, True)] + [inst(f"pod-{n}", good) for n in (2, 3, 4)]

    async def run():
        Coord.view = healthy
        await cluster_service.run_preflight()
        assert cluster_service.readiness() == (True, [])
        Coord.view = healthy + [inst("docker-auto", "deadbeefdeadbeef")]
        await cluster_service.run_preflight()
        return cluster_service.readiness()

    assert asyncio.run(run()) == (True, [])
    check = {c["id"]: c for c in cluster_service.preflight_results}["storage_secret"]
    assert check["status"] == "warn" and "docker-auto" in check["message"]


def test_storage_check_needs_fresh_peer_markers():
    pair = [_instance("a", ["api", "leader"], "s"), _instance("b", ["api", "leader"], "s")]
    stale = _by_id(asyncio.run(run_checks(_FakeClusterService(pair, markers={"a": 1.0, "b": 900.0}))))
    assert stale["storage"].status == "warn" and "b" in stale["storage"].message
    alone = _by_id(asyncio.run(run_checks(_FakeClusterService(pair[:1], markers={"a": 1.0}))))
    assert alone["storage"].status == "warn" and "only this instance" in alone["storage"].message


def test_storage_markers_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    cluster_service._write_storage_marker()
    markers = cluster_service.storage_markers()
    assert cluster_service.instance_id in markers and markers[cluster_service.instance_id] < 5
    assert (tmp_path / cluster_service.STORAGE_MARKER_DIR / cluster_service.instance_id).exists()


def test_readiness_reasons(monkeypatch):
    from core.components.boot.logic.boot_service import BootPhase

    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)
    monkeypatch.setattr(cluster_service, "draining", False)
    monkeypatch.setattr(cluster_service, "preflight_results", [])
    assert cluster_service.readiness() == (True, [])

    monkeypatch.setattr(db_instance, "is_connected", False)
    monkeypatch.setattr(cluster_service, "draining", True)
    monkeypatch.setattr(cluster_service, "preflight_results", [{"id": "storage_secret", "status": "fail"}, {"id": "x", "status": "warn"}])
    ok, reasons = cluster_service.readiness()
    assert ok is False and reasons == ["draining", "database", "preflight:storage_secret"]

    monkeypatch.setattr(boot_service, "phase", BootPhase.WAITING_CORE)
    monkeypatch.setattr(boot_service, "is_booting", True)
    assert "boot:waiting_core" in cluster_service.readiness()[1]


def test_snapshot_shape():
    snap = cluster_service.snapshot()
    assert snap["mode"] == "single" and snap["driver"] == "local"
    assert snap["instance"]["id"] == cluster_service.instance_id
    assert set(snap["leases"]) == {"held", "wanted"}
    assert "checks" in snap["preflight"] and "ok" in snap["preflight"]
