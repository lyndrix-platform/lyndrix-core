"""CL-16: the shared-storage check compares marker *contents* by name (no
directory listing, no file times), only among instances that serve user files,
ignores peers that just started, and says what a missing share means."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from core.components.cluster.logic import cluster_service as cs_mod
from core.components.cluster.logic.cluster_service import ClusterService
from core.components.cluster.logic.coordinator import InstanceInfo
from core.components.cluster.logic.preflight import Check, _check_storage


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _peer(iid, roles, incarnation, *, uptime_s=600, me=False):
    return InstanceInfo(iid, iid, list(roles), "1.12.0", "1.14.0", _now() - timedelta(seconds=uptime_s), _now(),
                        1.0, True, me, {}, state="alive", incarnation=incarnation)


def _service(tmp_path, monkeypatch, iid="pod-1", roles=("api", "leader")):
    """An instance whose STORAGE_DIR is ``tmp_path`` (settings are process-wide:
    one "current" instance per test; peers are simulated by their marker files)."""
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    return ClusterService(instance_id=iid, roles=list(roles), mode="cluster", driver="local", ttl=30,
                          heartbeat_s=10, subscribe=False, attach_bridge=False)


def _write_marker(directory, iid, content):
    marker_dir = os.path.join(str(directory), ClusterService.STORAGE_MARKER_DIR)
    os.makedirs(marker_dir, exist_ok=True)
    with open(os.path.join(marker_dir, iid), "w", encoding="utf-8") as fh:
        fh.write(content if isinstance(content, str) else json.dumps(content))
    return os.path.join(marker_dir, iid)


def _storage_check(svc, peers) -> Check:
    found: list = []

    def add(check_id, status, message, hint=None, *, recheck=False):
        found.append(Check(check_id, status, message, hint, recheck))

    me = _peer(svc.instance_id, svc.roles, svc.incarnation, me=True)
    asyncio.run(_check_storage(svc, [me, *peers], add))
    assert len(found) == 1 and found[0].id == "storage"
    return found[0]


def test_marker_names_the_writing_process(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc._write_storage_marker()
    with open(tmp_path / ClusterService.STORAGE_MARKER_DIR / "pod-1", encoding="utf-8") as fh:
        marker = json.load(fh)
    assert marker["instance_id"] == "pod-1" and marker["incarnation"] == svc.incarnation
    assert svc._read_peer_markers(["pod-1", "nobody"]) == {"pod-1": svc.incarnation, "nobody": None}


def test_shared_directory_is_recognised_by_marker_content_not_by_file_time(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    path = _write_marker(tmp_path, "pod-2", {"instance_id": "pod-2", "incarnation": "inc-2"})
    os.utime(path, (time.time() - 3600, time.time() - 3600))    # NFS server clock an hour off: irrelevant now
    check = _storage_check(svc, [_peer("pod-2", ["api", "leader"], "inc-2")])
    assert check.status == "ok" and "shared mount" in check.message and not check.recheck


def test_api_instance_on_another_host_is_named_with_what_it_means(tmp_path, monkeypatch):
    """Prod: the k3s pods share an RWX volume, docker-auto (api) and
    docker-auto-runner (runner) mount a host directory — a true positive for
    docker-auto, harmless for the runner."""
    svc = _service(tmp_path, monkeypatch)
    _write_marker(tmp_path, "pod-2", {"incarnation": "inc-2"})
    peers = [_peer("pod-2", ["api", "leader"], "inc-2"), _peer("docker-auto", ["api"], "inc-da"),
             _peer("docker-auto-runner", ["runner"], "inc-run")]
    check = _storage_check(svc, peers)
    assert check.status == "warn" and check.recheck
    assert "NOT shared with docker-auto (api)" in check.message and "docker-auto-runner" not in check.message
    assert "uploads" in check.hint and "LYNDRIX_STORAGE_SHARED_ROLES" in check.hint

    # only the runner is apart: fine, it serves no user files
    _write_marker(tmp_path, "docker-auto", {"incarnation": "inc-da"})
    check = _storage_check(svc, peers)
    assert check.status == "ok" and "markers of 2 peer(s) visible" in check.message
    assert "not shared with docker-auto-runner (runner) — fine" in check.message


def test_runner_only_instance_does_not_have_to_share(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, iid="docker-auto-runner", roles=("runner",))
    check = _storage_check(svc, [_peer("pod-1", ["api", "leader"], "inc-1"), _peer("pod-2", ["api"], "inc-2")])
    assert check.status == "ok" and "does not have to share" in check.message and "of its own" in check.message


def test_young_peers_and_leftover_markers(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    # a peer that registered seconds ago has not written its marker yet: no verdict, look again
    young = _storage_check(svc, [_peer("pod-9", ["api"], "inc-9", uptime_s=5)])
    assert young.status == "warn" and "just started" in young.message and young.recheck
    assert "NOT shared" not in young.message
    # a marker left by an earlier process under the same id proves nothing about today
    _write_marker(tmp_path, "docker-auto", {"incarnation": "an-old-process"})
    stale = _storage_check(svc, [_peer("docker-auto", ["api"], "inc-now")])
    assert stale.status == "warn" and "NOT shared with docker-auto" in stale.message
    # a core ≤ 1.11 peer writes a timestamp, no JSON: present under its name is all we can ask
    _write_marker(tmp_path, "old-pod", "2026-11-14T12:00:00+00:00")
    legacy = _storage_check(svc, [_peer("old-pod", ["api", "leader"], None)])
    assert legacy.status == "ok"
    alone = _storage_check(svc, [])
    assert alone.status == "warn" and "only this instance" in alone.message


def test_sharing_roles_are_configurable(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "LYNDRIX_STORAGE_SHARED_ROLES", "api,runner")
    check = _storage_check(svc, [_peer("docker-auto-runner", ["runner"], "inc-run")])
    assert check.status == "warn" and "NOT shared with docker-auto-runner (runner)" in check.message


def test_leader_removes_markers_of_instances_gone_for_a_day(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(svc, "_instances", [_peer("pod-2", ["api"], "inc-2")])
    old = time.time() - cs_mod._MARKER_MAX_AGE_S - 60
    for iid, age in (("pod-gone", old), ("pod-2", old), ("pod-recent", time.time() - 600)):
        os.utime(_write_marker(tmp_path, iid, {"incarnation": "x"}), (age, age))
    marker_dir = tmp_path / ClusterService.STORAGE_MARKER_DIR

    svc.is_leader = False
    svc._write_storage_marker()
    assert (marker_dir / "pod-gone").exists()                    # only the leader cleans up
    svc.is_leader = True
    svc._write_storage_marker()
    assert sorted(os.listdir(marker_dir)) == ["pod-1", "pod-2", "pod-recent"]   # known + recent ones stay
