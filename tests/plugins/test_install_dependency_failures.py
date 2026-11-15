"""BG-10: a plugin whose dependencies could not be installed is never swapped
in, the pip child is reaped on timeout, and a failed swap restores the old
version instead of leaving a partial directory plus an orphaned backup."""
import asyncio
import pathlib
import shutil
import sys

import pytest

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.plugins.logic import plugin_service as plugin_service_module
from core.components.plugins.logic.plugin_service import PluginInstallError, PluginService

sys.path.insert(0, str(pathlib.Path(__file__).parent))  # tests are not a package
from install_fakes import ENTRYPOINT, REPO_URL, FakePip, install_provider, plugin_archive, service_on


def _leftovers(svc):
    return sorted(p.name for p in svc.plugin_dir.iterdir())


def _install(svc, **kwargs):
    return asyncio.run(svc.install_plugin(REPO_URL, **kwargs))


def _existing_version(svc, version="1.0.0"):
    current = svc.plugin_dir / "sample_plugin"
    current.mkdir()
    (current / "entrypoint.py").write_text(ENTRYPOINT.format(version=version))
    return current


def test_successful_install_still_works(tmp_path, monkeypatch):
    pip = FakePip()
    svc = service_on(tmp_path, monkeypatch, pip)
    install_provider(monkeypatch, plugin_archive())

    assert _install(svc) is True
    assert (svc.plugin_dir / "sample_plugin" / "entrypoint.py").exists()
    assert len(pip.calls) == 1


def test_pip_failure_fails_the_install_and_creates_no_folder(tmp_path, monkeypatch):
    pip = FakePip(returncode=1, stderr=b"ERROR: No matching distribution found")
    svc = service_on(tmp_path, monkeypatch, pip)
    install_provider(monkeypatch, plugin_archive())

    assert _install(svc) is False
    assert _leftovers(svc) == []
    # ...so a retry is not skipped as "already installed".
    pip.returncode_on_exit = 0
    assert _install(svc) is True


def test_blocked_requirement_fails_the_install_without_running_pip(tmp_path, monkeypatch):
    pip = FakePip()
    svc = service_on(tmp_path, monkeypatch, pip)
    install_provider(monkeypatch, plugin_archive(requirements="--index-url https://evil.example/simple\nx\n"))

    assert _install(svc) is False
    assert pip.calls == []
    assert _leftovers(svc) == []


def test_pip_timeout_kills_and_reaps_the_process(tmp_path, monkeypatch):
    pip = FakePip(hang=True)
    svc = service_on(tmp_path, monkeypatch, pip)
    plugin_dir = tmp_path / "staged"
    plugin_dir.mkdir()
    (plugin_dir / "requirements.txt").write_text("deepmerge==1.1.1\n")

    with pytest.raises(PluginInstallError, match="timed out"):
        asyncio.run(svc._install_requirements(plugin_dir, timeout=0.05))

    assert pip.killed and pip.waited
    assert not (plugin_dir / "vendor").exists()


def test_failed_upgrade_keeps_the_running_version(tmp_path, monkeypatch):
    pip = FakePip(returncode=1)
    svc = service_on(tmp_path, monkeypatch, pip)
    _existing_version(svc)
    install_provider(monkeypatch, plugin_archive(version="2.0.0"))

    assert _install(svc, upgrade=True) is False
    assert _leftovers(svc) == ["sample_plugin"]
    assert svc._read_manifest_version(svc.plugin_dir / "sample_plugin") == "1.0.0"


def test_failed_swap_removes_the_partial_copy_and_restores_the_backup(tmp_path, monkeypatch):
    svc = service_on(tmp_path, monkeypatch, FakePip())
    _existing_version(svc)
    install_provider(monkeypatch, plugin_archive(version="2.0.0"))

    def half_move(src, dst):
        # A cross-filesystem move that dies midway (disk full): part of the
        # new tree is already at the destination.
        shutil.copytree(src, dst)
        (plugin_service_module.Path(dst) / "entrypoint.py").unlink()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(plugin_service_module.shutil, "move", half_move)

    assert _install(svc, upgrade=True) is False
    assert _leftovers(svc) == ["sample_plugin"]          # no .backup_ orphan
    assert svc._read_manifest_version(svc.plugin_dir / "sample_plugin") == "1.0.0"


def test_stale_backup_is_not_restored_over_a_working_plugin(tmp_path):
    plugins = tmp_path
    current = plugins / "sample"
    current.mkdir()
    (current / "entrypoint.py").write_text(ENTRYPOINT.format(version="3.0.0"))
    stale = plugins / ".backup_sample"
    stale.mkdir()
    (stale / "entrypoint.py").write_text(ENTRYPOINT.format(version="0.9.0"))

    # Failure before the swap started (e.g. pip): the current version stays.
    PluginService._restore_after_failed_install(current, stale, True, False)

    assert PluginService()._read_manifest_version(current) == "3.0.0"


# --- SC-03 remainder: identity before dependencies -------------------------------

def test_identity_mismatch_aborts_the_upgrade_before_pip_runs(tmp_path, monkeypatch):
    pip = FakePip()
    svc = service_on(tmp_path, monkeypatch, pip)
    current = _existing_version(svc)
    # The installed plugin has another manifest id than the downloaded archive.
    (current / "entrypoint.py").write_text(
        ENTRYPOINT.format(version="1.0.0").replace("lyndrix.plugin.sample", "lyndrix.plugin.other"))
    install_provider(monkeypatch, plugin_archive(version="2.0.0"))

    assert _install(svc, upgrade=True) is False
    assert pip.calls == []                       # no dependency code was fetched or built
    assert _leftovers(svc) == ["sample_plugin"]
    assert svc._read_manifest_version(current) == "1.0.0"


def test_matching_identity_still_installs_dependencies_and_upgrades(tmp_path, monkeypatch):
    pip = FakePip()
    svc = service_on(tmp_path, monkeypatch, pip)
    _existing_version(svc)
    install_provider(monkeypatch, plugin_archive(version="2.0.0"))

    assert _install(svc, upgrade=True) is True
    assert len(pip.calls) == 1
    assert svc._read_manifest_version(svc.plugin_dir / "sample_plugin") == "2.0.0"
    assert svc.previous_version("sample_plugin") == "1.0.0"
