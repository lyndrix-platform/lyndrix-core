"""BG-15: a plugin upgrade must serve the NEW strings.

The locale directory of a reloaded plugin counted as "already scanned": the
catalog version changed with the new files' mtimes, clients refetched and were
served the old strings under the new version until the core restarted.
"""
import json
import types

import pytest

import core.services  # noqa: F401  (import order: services before api)
from core import i18n
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

NS = "bg15_probe"


def _write(plugin_dir, strings: dict, locale: str = "en") -> None:
    (plugin_dir / "locales").mkdir(exist_ok=True)
    (plugin_dir / "locales" / f"{NS}.{locale}.json").write_text(json.dumps(strings), encoding="utf-8")


@pytest.fixture()
def plugin_dir(tmp_path):
    try:
        yield tmp_path
    finally:
        i18n.unregister_plugin_locales(tmp_path)
        i18n.unregister_client_namespace(NS)


def test_reregistering_after_unregister_serves_the_new_strings(plugin_dir):
    _write(plugin_dir, {"hello": "old", "gone": "x"})
    i18n.register_plugin_locales(plugin_dir)
    i18n.register_client_namespace(NS)
    v1 = i18n.get_catalog_version()
    assert i18n.get_catalog("en", [NS])[NS] == {"hello": "old", "gone": "x"}

    i18n.unregister_plugin_locales(plugin_dir)          # unload
    assert i18n.get_catalog("en", [NS])[NS] == {} and i18n.t(f"{NS}.hello", locale="en") == f"{NS}.hello"

    _write(plugin_dir, {"hello": "new", "added": "y"})  # the upgrade swapped the files
    i18n.register_plugin_locales(plugin_dir)            # reload
    assert i18n.get_catalog("en", [NS])[NS] == {"hello": "new", "added": "y"}
    assert i18n.t(f"{NS}.hello", locale="en") == "new"
    assert i18n.get_catalog_version() != v1


def test_unregister_keeps_the_core_catalogue(plugin_dir):
    core_key_before = i18n.t("core.nav.dashboard", locale="en")
    _write(plugin_dir, {"hello": "x"})
    i18n.register_plugin_locales(plugin_dir)
    i18n.unregister_plugin_locales(plugin_dir)
    assert i18n.t("core.nav.dashboard", locale="en") == core_key_before != "core.nav.dashboard"
    assert "ui" in i18n.get_catalog("en", ["ui"]) and i18n.get_catalog("en", ["ui"])["ui"]


def test_version_covers_the_client_namespace_allowlist(plugin_dir):
    """Opting a namespace in/out without a file change must not yield a 304."""
    v0 = i18n.get_catalog_version()
    i18n.register_client_namespace(NS)
    v1 = i18n.get_catalog_version()
    assert v1 != v0 and NS in i18n.client_namespaces()
    i18n.unregister_client_namespace(NS)
    assert i18n.get_catalog_version() == v0 and NS not in i18n.client_namespaces()

    i18n.unregister_client_namespace("ui")              # core namespaces stay
    assert "ui" in i18n.client_namespaces()


def test_unload_module_drops_the_plugins_locales_and_namespace(plugin_dir, monkeypatch):
    _write(plugin_dir, {"hello": "old"})
    i18n.register_plugin_locales(plugin_dir)
    i18n.register_client_namespace(NS)

    mgr = ModuleManager()
    monkeypatch.setattr(mgr, "_teardown_ui", lambda mid: None)
    manifest = ModuleManifest(id="lyndrix.plugin.bg15_probe", name="BG15", version="0.0.1", type="PLUGIN",
                              permissions=ModulePermissions(emit=[], subscribe=[]), i18n_namespace=NS)
    module = types.SimpleNamespace(__name__="plugins.bg15_probe.entrypoint",
                                   __file__=str(plugin_dir / "entrypoint.py"))
    mgr.registry[manifest.id] = {"manifest": manifest, "module": module, "context": None,
                                 "status": "active", "locales_path": plugin_dir}

    assert mgr.unload_module(manifest.id) is True
    assert i18n.get_catalog("en", [NS])[NS] == {}
    assert NS not in i18n.client_namespaces()
