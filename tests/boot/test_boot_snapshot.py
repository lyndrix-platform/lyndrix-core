"""Tests for ``BootService.snapshot`` — the dependency list behind ``GET /api/boot``
and the NiceGUI boot page.

Run inside the lyndrix-core runtime:
    pytest tests/boot/test_boot_snapshot.py -v
"""
import pytest

from config import settings
from core.components.boot.logic.boot_service import (
    ACTION,
    CLUSTER_SERVICES,
    CORE_SERVICES,
    FAILED,
    ONLINE,
    PENDING,
    UNAVAILABLE,
    WAITING,
    BootPhase,
    BootService,
)
from core.services import db_instance, vault_instance


def _by_id(snap):
    return {s["id"]: s for s in snap["services"]}


@pytest.fixture()
def svc(monkeypatch):
    """A fresh BootService with every dependency reset to 'nothing happened yet'."""
    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "single")
    monkeypatch.setattr(vault_instance, "ui_state", "loading")
    monkeypatch.setattr(db_instance, "is_connected", False)
    monkeypatch.setattr(db_instance, "connect_attempts", 0)
    monkeypatch.setattr(db_instance, "failure_reason", None)
    monkeypatch.setattr(db_instance, "was_connected", False)
    return BootService()


def test_initial_snapshot_everything_waits_on_vault(svc):
    snap = svc.snapshot()
    rows = _by_id(snap)
    assert snap["mode"] == "single"
    assert snap["is_booting"] is True and snap["ready"] is False
    assert [s["id"] for s in snap["services"]] == list(CORE_SERVICES)
    assert rows["vault"]["status"] == PENDING and rows["vault"]["detail"] == "connecting"
    assert rows["database"]["status"] == WAITING
    assert rows["database"]["detail_params"] == {"service": "vault"}
    assert rows["iam"]["status"] == WAITING
    assert rows["modules"]["status"] == WAITING
    assert snap["summary"] == {"online": 0, "total": 4}
    assert all(s["since"] for s in snap["services"])


def test_vault_needs_unseal_is_an_operator_action(svc, monkeypatch):
    monkeypatch.setattr(vault_instance, "ui_state", "needs_unseal")
    rows = _by_id(svc.snapshot())
    assert rows["vault"]["status"] == ACTION and rows["vault"]["detail"] == "sealed"
    assert svc.snapshot()["vault_ui_state"] == "needs_unseal"


def test_database_retry_progress_is_visible(svc, monkeypatch):
    monkeypatch.setattr(vault_instance, "ui_state", "ready")
    monkeypatch.setattr(db_instance, "connect_attempts", 3)
    rows = _by_id(svc.snapshot())
    assert rows["vault"]["status"] == ONLINE
    db = rows["database"]
    assert db["status"] == PENDING and db["detail"] == "db_retry"
    assert db["detail_params"]["attempt"] == 3
    assert db["detail_params"]["max"] == db_instance.max_retries
    assert rows["iam"]["status"] == WAITING


def test_database_gave_up_reads_as_failed(svc, monkeypatch):
    monkeypatch.setattr(vault_instance, "ui_state", "ready")
    monkeypatch.setattr(db_instance, "failure_reason", "config_error")
    assert _by_id(svc.snapshot())["database"]["status"] == FAILED
    monkeypatch.setattr(db_instance, "failure_reason", "max_retries")
    db = _by_id(svc.snapshot())["database"]
    assert db["status"] == FAILED and db["detail"] == "db_gave_up"


def test_watchdog_drop_reads_as_reconnecting(svc, monkeypatch):
    monkeypatch.setattr(vault_instance, "ui_state", "ready")
    monkeypatch.setattr(db_instance, "was_connected", True)
    monkeypatch.setattr(db_instance, "connect_attempts", 2)
    assert _by_id(svc.snapshot())["database"]["detail"] == "reconnecting"


def test_iam_failure_is_reported(svc, monkeypatch):
    monkeypatch.setattr(vault_instance, "ui_state", "ready")
    monkeypatch.setattr(db_instance, "is_connected", True)
    assert _by_id(svc.snapshot())["iam"]["status"] == PENDING
    svc.iam_error = "boom"
    iam = _by_id(svc.snapshot())["iam"]
    assert iam["status"] == FAILED and iam["detail"] == "iam_failed"
    # Error text never leaks into the public payload.
    assert "boom" not in str(svc.snapshot())


def test_everything_online_when_boot_completes(svc, monkeypatch):
    monkeypatch.setattr(vault_instance, "ui_state", "ready")
    monkeypatch.setattr(db_instance, "is_connected", True)
    svc.iam_ready = True
    svc.phase = BootPhase.READY
    svc.is_booting = False
    snap = svc.snapshot()
    assert snap["ready"] is True and snap["failed"] is False
    assert all(s["status"] == ONLINE for s in snap["services"])
    assert snap["summary"]["online"] == snap["summary"]["total"] == 4
    modules = _by_id(snap)["modules"]
    assert modules["detail"] == "modules_summary"
    assert set(modules["detail_params"]) == {"active", "failed", "total"}


def test_boot_failure_marks_modules_failed(svc):
    svc.phase = BootPhase.FAILED
    svc.is_booting = False
    snap = svc.snapshot()
    assert snap["failed"] is True
    assert _by_id(snap)["modules"]["status"] == FAILED


def test_cluster_mode_lists_cluster_dependencies(svc, monkeypatch):
    from core.components.cluster.logic.cluster_service import cluster_service

    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "cluster")
    monkeypatch.setattr(cluster_service, "started", False)
    monkeypatch.setattr(cluster_service, "start_error", None)
    monkeypatch.setattr(cluster_service, "is_leader", False)
    monkeypatch.setattr(cluster_service, "leader_id", None)
    snap = svc.snapshot()
    rows = _by_id(snap)
    assert snap["mode"] == "cluster" and snap["instance_id"] == cluster_service.instance_id
    assert [s["id"] for s in snap["services"]] == list(CORE_SERVICES) + list(CLUSTER_SERVICES)
    # DB not connected yet → the coordinator waits for it, the rest waits for the coordinator.
    assert rows["coordinator"]["status"] == WAITING and rows["coordinator"]["detail_params"] == {"service": "database"}
    assert rows["leader"]["status"] == WAITING and rows["peers"]["status"] == WAITING
    assert rows["storage"]["status"] == UNAVAILABLE and rows["storage"]["detail"] == "storage_not_available"
    assert snap["summary"]["total"] == 8

    # Coordinator up, this instance leads, one peer alive.
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(cluster_service, "started", True)
    monkeypatch.setattr(cluster_service, "is_leader", True)
    monkeypatch.setattr(cluster_service, "leader_id", cluster_service.instance_id)
    peer = type("I", (), {"alive": True})()
    monkeypatch.setattr(cluster_service, "_instances", [peer, peer])
    rows = _by_id(svc.snapshot())
    assert rows["coordinator"]["status"] == ONLINE and rows["coordinator"]["detail"] == "coordinator_connected"
    assert rows["leader"]["status"] == ONLINE and rows["leader"]["detail"] == "leader_self"
    assert rows["peers"]["status"] == ONLINE and rows["peers"]["detail_params"] == {"peers": 1, "total": 2}

    # Another instance leads.
    monkeypatch.setattr(cluster_service, "is_leader", False)
    monkeypatch.setattr(cluster_service, "leader_id", "peer-b")
    row = _by_id(svc.snapshot())["leader"]
    assert row["detail"] == "leader_other" and row["detail_params"] == {"instance": "peer-b"}


def test_since_is_stamped_on_status_change(svc, monkeypatch):
    first = _by_id(svc.snapshot())["vault"]["since"]
    assert _by_id(svc.snapshot())["vault"]["since"] == first  # unchanged status keeps its stamp
    monkeypatch.setattr(vault_instance, "ui_state", "ready")
    assert _by_id(svc.snapshot())["vault"]["since"] >= first


def test_cluster_mode_setting_is_validated():
    from pydantic import ValidationError

    Settings = type(settings)
    s = Settings(LYNDRIX_CLUSTER_MODE="Cluster", LYNDRIX_COORDINATOR="db", LYNDRIX_INSTANCE_ROLES=None,
                 LYNDRIX_DRAIN_SECONDS=None)
    assert s.LYNDRIX_CLUSTER_MODE == "cluster" and s.is_cluster
    assert s.instance_roles == ["api", "leader"] and s.LYNDRIX_DRAIN_SECONDS == 10  # cluster defaults
    single = Settings(LYNDRIX_CLUSTER_MODE="single", LYNDRIX_COORDINATOR=None, LYNDRIX_INSTANCE_ROLES=None,
                      LYNDRIX_DRAIN_SECONDS=None)
    assert single.LYNDRIX_COORDINATOR == "local" and single.instance_roles == ["api", "leader", "runner"]
    assert single.LYNDRIX_DRAIN_SECONDS == 0
    with pytest.raises(ValidationError):
        Settings(LYNDRIX_CLUSTER_MODE="swarm")
    with pytest.raises(ValidationError):  # cluster needs a shared coordinator
        Settings(LYNDRIX_CLUSTER_MODE="cluster", LYNDRIX_COORDINATOR="local")
    with pytest.raises(ValidationError):  # unknown role
        Settings(LYNDRIX_INSTANCE_ROLES="api,worker")
    with pytest.raises(ValidationError):  # heartbeat must fit twice into the TTL
        Settings(LYNDRIX_LEASE_TTL_SECONDS=10, LYNDRIX_LEASE_HEARTBEAT_SECONDS=8)
    assert Settings(VAULT_NODES="https://v1:8200, https://v2:8200/").vault_nodes == ["https://v1:8200", "https://v2:8200"]
