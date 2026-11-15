"""SC-01: the encrypted Vault key blob and its directory are owner-only; the
entrypoint no longer makes secrets or plugin code world-writable."""
import os
import pathlib
import re
import stat

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.vault.logic import crypto

ENTRYPOINT = pathlib.Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_key_file_is_written_owner_only_even_with_open_umask(tmp_path):
    target = tmp_path / "security" / "vault_keys.enc"
    previous = os.umask(0)
    try:
        crypto.write_key_file(b"blob-one", str(target))
    finally:
        os.umask(previous)
    assert target.read_bytes() == b"blob-one"
    assert _mode(target) == 0o600
    assert _mode(target.parent) == 0o700


def test_key_file_rewrite_is_atomic_and_keeps_mode(tmp_path):
    target = tmp_path / "vault_keys.enc"
    target.write_bytes(b"old")
    os.chmod(target, 0o666)
    crypto.write_key_file(b"new", str(target))
    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["vault_keys.enc"]  # no temp leftovers


def test_boot_hardening_tightens_a_legacy_key_file(tmp_path):
    directory = tmp_path / "security"
    directory.mkdir()
    target = directory / "vault_keys.enc"
    target.write_bytes(b"legacy")
    os.chmod(target, 0o644)
    os.chmod(directory, 0o777)

    crypto.harden_key_storage(str(target))

    assert _mode(target) == 0o600
    assert _mode(directory) == 0o700


def test_boot_hardening_tolerates_a_missing_file(tmp_path):
    crypto.harden_key_storage(str(tmp_path / "missing" / "vault_keys.enc"))


def test_setup_fresh_vault_uses_the_owner_only_writer(tmp_path, monkeypatch):
    from core.components.vault.logic import vault_init

    written = {}

    class _Sys:
        def is_initialized(self):
            return False

        def initialize(self, **_kwargs):
            return {"root_token": "dummy-root-not-real", "keys": ["dummy-unseal-not-real"]}

    class _Client:
        sys = _Sys()

    target = tmp_path / "vault_keys.enc"
    monkeypatch.setattr(vault_init.VaultInitializer, "get_client", lambda self: _Client())
    monkeypatch.setattr(vault_init, "encrypt_vault_keys", lambda key, keys: b"encrypted")
    monkeypatch.setattr(
        vault_init, "write_key_file",
        lambda blob: (written.setdefault("blob", blob), crypto.write_key_file(blob, str(target))),
    )

    vault_init.VaultInitializer("http://vault.invalid").setup_fresh_vault("dummy-master-key-not-real")

    assert written["blob"] == b"encrypted"
    assert _mode(target) == 0o600


def test_entrypoint_sets_no_world_writable_mode():
    script = ENTRYPOINT.read_text()
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"chmod\s+(-R\s+)?0?[0-7]?[0-7][0-7][2367]\b", code)
    assert "chmod 700 /data/security" in code
    assert "chmod 600 /data/security/vault_keys.enc" in code
