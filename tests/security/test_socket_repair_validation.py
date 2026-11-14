"""SC-15: /api/socket/repair validates the owner and only applies safe modes."""
import asyncio

import pytest
from fastapi import HTTPException

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.sockets.api import socket_api
from core.components.sockets.logic import mount_guardian
from core.components.sockets.logic.mount_guardian import MountGuardian, is_valid_owner


class _RecordingGuardian:
    def __init__(self):
        self.calls = []

    def repair_permissions(self, target, user, mode):
        self.calls.append((target, user, mode))
        return None


@pytest.fixture
def managed_dirs(tmp_path, monkeypatch):
    storage, security = tmp_path / "storage", tmp_path / "security"
    storage.mkdir()
    security.mkdir()
    monkeypatch.setattr(socket_api.settings, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(socket_api.settings, "SECURITY_DIR", str(security))
    guardian = _RecordingGuardian()
    monkeypatch.setattr(socket_api, "get_mount_guardian", lambda: guardian)
    return storage, security, guardian


def _repair(**body):
    return asyncio.run(socket_api.repair_mount_permissions(socket_api.RepairRequest(**body), auth=None))


@pytest.mark.parametrize("user", ["root", "www-data", "_svc", "1000", "1000:1000", "app:app"])
def test_valid_owners(user):
    assert is_valid_owner(user)


@pytest.mark.parametrize("user", ["", "-R", "--reference=/etc/shadow", "root;id", "a b", "Root", "1000:", "../x", "a" * 40, "root\n"])
def test_invalid_owners(user):
    assert not is_valid_owner(user)


def test_option_like_user_is_rejected_before_any_subprocess(managed_dirs):
    storage, _security, guardian = managed_dirs
    with pytest.raises(HTTPException) as exc:
        _repair(target_dir=str(storage), user="--reference=/etc/shadow")
    assert exc.value.status_code == 422
    assert guardian.calls == []


@pytest.mark.parametrize("mode", ["777", "666", "757", "1777", "4755", "000"])
def test_unsafe_modes_are_rejected(managed_dirs, mode):
    storage, _security, guardian = managed_dirs
    with pytest.raises(HTTPException) as exc:
        _repair(target_dir=str(storage), mode=mode)
    assert exc.value.status_code == 422
    assert guardian.calls == []


def test_security_dir_only_accepts_owner_only(managed_dirs):
    _storage, security, guardian = managed_dirs
    with pytest.raises(HTTPException) as exc:
        _repair(target_dir=str(security), mode="755")
    assert exc.value.status_code == 422
    assert _repair(target_dir=str(security), mode="700") == {"status": "repaired"}
    assert guardian.calls == [(str(security.resolve()), "root", 0o700)]


def test_default_request_still_works(managed_dirs):
    storage, _security, guardian = managed_dirs
    assert _repair(target_dir=str(storage)) == {"status": "repaired"}
    assert guardian.calls == [(str(storage.resolve()), "root", 0o755)]


def test_guardian_refuses_unsafe_input_on_its_own(tmp_path, monkeypatch):
    # Defence in depth: the guardian is also reachable without the API route.
    monkeypatch.setattr(mount_guardian.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    guardian = MountGuardian()
    assert "Invalid owner" in guardian.repair_permissions(str(tmp_path), "-R", 0o755)
    assert "Mode not allowed" in guardian.repair_permissions(str(tmp_path), "root", 0o777)


def test_guardian_passes_arguments_after_double_dash(tmp_path, monkeypatch):
    commands = []

    class _Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(mount_guardian.subprocess, "run", lambda cmd, **k: (commands.append(cmd), _Done())[1])
    monkeypatch.setattr(mount_guardian.os, "geteuid", lambda: 0)
    assert MountGuardian().repair_permissions(str(tmp_path), "1000:1000", 0o750) is None
    assert commands == [
        ["chown", "-R", "--", "1000:1000", str(tmp_path)],
        ["chmod", "-R", "--", "750", str(tmp_path)],
    ]
