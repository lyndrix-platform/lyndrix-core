"""Unloading a plugin must forget its ORM models on the shared Base, otherwise
re-importing the same folder (reload, auto-revert, cluster reconcile) fails with
"Table '…' is already defined for this MetaData instance".

Run inside the lyndrix-core runtime:
    pytest tests/plugins/test_unload_models.py -v
"""
from sqlalchemy import Column, Integer, String

from core.components.database.logic.db_service import Base
from core.components.plugins.logic.manager import ModuleManager

PKG = "plugins.fake_unload_plugin"


def _define_model():
    # Simulates `plugins/fake_unload_plugin/app/model/models.py` being imported.
    attrs = {
        "__tablename__": "fake_unload_items",
        "__module__": f"{PKG}.app.model.models",
        "id": Column(Integer, primary_key=True),
        "name": Column(String(50)),
    }
    return type("FakeUnloadItem", (Base,), attrs)


def test_forget_plugin_models_allows_reimport():
    first = _define_model()
    assert "fake_unload_items" in Base.metadata.tables
    assert any(m.class_ is first for m in Base.registry.mappers)

    removed = ModuleManager._forget_plugin_models(PKG)
    assert removed == 1
    assert "fake_unload_items" not in Base.metadata.tables
    assert not any(m.class_ is first for m in Base.registry.mappers)

    # The "re-import" defines the same table again — this used to raise.
    second = _define_model()
    assert second is not first and "fake_unload_items" in Base.metadata.tables
    ModuleManager._forget_plugin_models(PKG)  # cleanup


def test_forget_plugin_models_ignores_other_packages():
    model = _define_model()
    try:
        assert ModuleManager._forget_plugin_models("plugins.some_other_plugin") == 0
        assert "fake_unload_items" in Base.metadata.tables
        assert any(m.class_ is model for m in Base.registry.mappers)
    finally:
        ModuleManager._forget_plugin_models(PKG)


def test_unload_keeps_the_modules_of_a_sibling_package(monkeypatch):
    """BG-16: unloading `plugins.bg16_mon` purged `plugins.bg16_mon_agent.*` from
    sys.modules too (bare startswith) — while that plugin was running."""
    import sys
    import types

    from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

    for name in ("plugins.bg16_mon", "plugins.bg16_mon.entrypoint", "plugins.bg16_mon.app",
                 "plugins.bg16_mon_agent", "plugins.bg16_mon_agent.entrypoint"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    mgr = ModuleManager()
    monkeypatch.setattr(mgr, "_teardown_ui", lambda mid: None)
    module = sys.modules["plugins.bg16_mon.entrypoint"]
    module.__file__ = "/nonexistent/plugins/bg16_mon/entrypoint.py"
    manifest = ModuleManifest(id="lyndrix.plugin.bg16_mon", name="Mon", version="0.0.1", type="PLUGIN",
                              permissions=ModulePermissions(emit=[], subscribe=[]))
    mgr.registry[manifest.id] = {"manifest": manifest, "module": module, "context": None, "status": "active"}

    assert mgr.unload_module(manifest.id) is True
    assert not [m for m in sys.modules if m == "plugins.bg16_mon" or m.startswith("plugins.bg16_mon.")]
    assert "plugins.bg16_mon_agent" in sys.modules and "plugins.bg16_mon_agent.entrypoint" in sys.modules
