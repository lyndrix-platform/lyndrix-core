"""BG-07: a failed plugin import must leave nothing behind.

Python removes only the module that raised; the package, the submodules the
entrypoint imported before the error and their ORM classes on the shared
``Base`` used to stay. The auto-revert then loaded the restored OLD entrypoint
on top of the cached NEW submodules.
"""
import sys

import pytest

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic.db_service import Base
from core.components.plugins.logic.manager import ModuleManager

NAME = "bg07_purge_probe"
PKG = f"plugins.{NAME}"

MODEL_SRC = (
    "from sqlalchemy import Column, Integer\n"
    "from core.components.database.logic.db_service import Base\n"
    "REVISION = '{rev}'\n"
    "class Bg07Row(Base):\n"
    "    __tablename__ = '{table}'\n"
    "    id = Column(Integer, primary_key=True)\n"
)
ENTRYPOINT_OK = (
    "from core.api import ModuleManifest\n"
    "from .app import model\n"
    "manifest = ModuleManifest(id='lyndrix.plugin.bg07_purge_probe', name='BG07', version='{version}',\n"
    "                          type='PLUGIN', description='probe', icon='science')\n"
    "def setup(ctx): ...\n"
)


@pytest.fixture()
def plugin_tree(tmp_path, monkeypatch):
    """A throw-away ``plugins/<name>`` tree the manager imports from."""
    root = tmp_path / "plugins" / NAME
    (root / "app").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "app" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    # The tests rewrite a source file within the same second and with the same
    # size — a cached .pyc would be considered fresh and hide the new source.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    # ``plugins`` may be cached as a namespace package of another location.
    monkeypatch.delitem(sys.modules, "plugins", raising=False)
    mgr = ModuleManager()
    mgr.base_path = str(tmp_path)
    try:
        yield mgr, root
    finally:
        for mid in list(mgr.registry):
            mgr.registry.pop(mid)
        ModuleManager._forget_plugin_models(PKG)
        for name in [m for m in sys.modules if m == PKG or m.startswith(PKG + ".")]:
            sys.modules.pop(name, None)
        sys.modules.pop("plugins", None)


def _write(root, *, table: str, rev: str, entrypoint: str) -> None:
    (root / "app" / "model.py").write_text(MODEL_SRC.format(table=table, rev=rev))
    (root / "entrypoint.py").write_text(entrypoint)


def test_failed_import_purges_submodules_and_orm_classes(plugin_tree):
    mgr, root = plugin_tree
    # NEW version: imports its model, then blows up.
    _write(root, table="bg07_rows_new", rev="new",
           entrypoint="from .app import model\nraise RuntimeError('broken new version')\n")

    assert mgr.load_module(NAME, is_plugin=True) is False
    assert not [m for m in sys.modules if m == PKG or m.startswith(PKG + ".")]
    assert "bg07_rows_new" not in Base.metadata.tables

    # Auto-revert restores the OLD files and loads them: old code, old table.
    _write(root, table="bg07_rows_old", rev="old", entrypoint=ENTRYPOINT_OK.format(version="1.0.0"))
    assert mgr.load_module(NAME, is_plugin=True) is True
    assert sys.modules[f"{PKG}.app.model"].REVISION == "old"
    assert "bg07_rows_old" in Base.metadata.tables and "bg07_rows_new" not in Base.metadata.tables


def test_entrypoint_without_manifest_is_reread_after_a_fix(plugin_tree):
    mgr, root = plugin_tree
    _write(root, table="bg07_rows_a", rev="a", entrypoint="from .app import model\n")
    assert mgr.load_module(NAME, is_plugin=True) is False
    assert f"{PKG}.entrypoint" not in sys.modules and "bg07_rows_a" not in Base.metadata.tables

    _write(root, table="bg07_rows_a", rev="b", entrypoint=ENTRYPOINT_OK.format(version="1.0.1"))
    assert mgr.load_module(NAME, is_plugin=True) is True


def test_duplicate_load_of_a_registered_plugin_keeps_its_modules(plugin_tree):
    """ALREADY_REGISTERED is not a failed import — the live modules must stay."""
    mgr, root = plugin_tree
    _write(root, table="bg07_rows_live", rev="live", entrypoint=ENTRYPOINT_OK.format(version="1.0.0"))
    assert mgr.load_module(NAME, is_plugin=True) is True
    live = sys.modules[f"{PKG}.app.model"]

    assert mgr.load_module(NAME, is_plugin=True) is False
    assert sys.modules[f"{PKG}.app.model"] is live and "bg07_rows_live" in Base.metadata.tables
