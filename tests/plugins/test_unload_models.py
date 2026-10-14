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
