"""Regression tests for the safe plugin update + rollback lifecycle.

Covers the pieces added in the lifecycle overhaul:
  * manifest-id / version static extraction (used by id-based reload + history)
  * retained `.previous_<repo>` restore point + restore_previous() swap
  * the post-install health gate (_verify_after_install)

Run inside the lyndrix-core runtime (deps + a writable /app), e.g.:
    pytest tests/plugins/test_plugin_lifecycle.py -v
"""
import asyncio
import textwrap

import pytest

from core.components.plugins.logic.plugin_service import PluginService
from core.components.plugins.logic.manager import ModuleManager


def _write_plugin(dir_path, plugin_id="lyndrix.plugin.sample", version="1.0.0"):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "entrypoint.py").write_text(
        textwrap.dedent(
            f'''
            from core.api import ModuleManifest
            manifest = ModuleManifest(
                id="{plugin_id}", name="Sample", version="{version}", type="PLUGIN",
            )
            def setup(ctx): ...
            '''
        ),
        encoding="utf-8",
    )
    return dir_path


# --- static manifest extraction -------------------------------------------------

def test_extract_manifest_id_and_version(tmp_path):
    ep = _write_plugin(tmp_path / "p", plugin_id="lyndrix.plugin.x", version="2.3.4") / "entrypoint.py"
    assert PluginService._extract_manifest_field(str(ep), "id") == "lyndrix.plugin.x"
    assert PluginService._extract_manifest_field(str(ep), "version") == "2.3.4"
    assert PluginService._extract_manifest_id(str(ep)) == "lyndrix.plugin.x"


# --- retained restore point + revert -------------------------------------------

def _service_on(tmp_path):
    svc = PluginService()
    svc.plugin_dir = tmp_path  # redirect away from the real app/plugins dir
    return svc


def test_has_previous_false_when_absent(tmp_path):
    svc = _service_on(tmp_path)
    assert svc.has_previous("sample") is False
    assert svc.previous_version("sample") is None
    assert svc.restore_previous("sample") is False  # nothing to restore


def test_restore_previous_swaps_in_old_version(tmp_path):
    svc = _service_on(tmp_path)
    # current = broken new version; .previous_ = last good version
    _write_plugin(tmp_path / "sample", version="2.0.0")
    _write_plugin(tmp_path / ".previous_sample", version="1.0.0")

    assert svc.has_previous("sample") is True
    assert svc.previous_version("sample") == "1.0.0"

    assert svc.restore_previous("sample") is True
    # current is now the old good version, and the restore point is consumed
    assert svc._read_manifest_version(tmp_path / "sample") == "1.0.0"
    assert not (tmp_path / ".previous_sample").exists()
    assert not (tmp_path / ".failed_sample").exists()


# --- post-install health gate ---------------------------------------------------

def _mgr():
    return ModuleManager()


def test_verify_passes_without_health(tmp_path):
    mgr = _mgr()
    mgr.registry["x"] = {"status": "active", "module": object(), "context": object()}
    assert asyncio.run(mgr._verify_after_install("x")) is True


def test_verify_fails_when_failed_status(tmp_path):
    mgr = _mgr()
    mgr.registry["x"] = {"status": "failed", "module": object(), "context": object()}
    assert asyncio.run(mgr._verify_after_install("x")) is False


def test_verify_fails_when_health_reports_error(tmp_path):
    mgr = _mgr()

    class _Mod:
        def health(self, ctx):
            class R:
                status = "error"
                details = {"reason": "boom"}
            return R()

    mgr.registry["x"] = {"status": "active", "module": _Mod(), "context": object()}
    assert asyncio.run(mgr._verify_after_install("x")) is False


def test_verify_fails_when_not_loaded(tmp_path):
    mgr = _mgr()
    assert asyncio.run(mgr._verify_after_install("missing")) is False
