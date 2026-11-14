"""BG-22: ``LYNDRIX_PLUGINS_DESIRED`` pins are written to the plugin's own row.

The row used to be matched with ``module_id LIKE '%<repo_name>%'`` — a substring
match in which ``_`` is a wildcard — and ``.first()``: the pin of
``lyndrix-plugin-ai`` could rewrite the row of ``lyndrix.plugin.ai_assistant``.
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import db_service
from core.components.plugins.logic import plugin_service as ps_mod
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import PluginState

AI_URL = "https://github.com/lyndrix-platform/lyndrix-plugin-ai"
ASSISTANT_URL = "https://github.com/lyndrix-platform/lyndrix-plugin-ai-assistant"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginState.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    plugin_dir = tmp_path / "plugins"       # <base_path>/plugins, as the manager expects
    plugin_dir.mkdir()
    monkeypatch.setattr(ps_mod.plugin_service, "plugin_dir", plugin_dir)
    installs = []

    async def fake_install(url, version="latest", upgrade=False, notify=True, lead=True):
        installs.append((url, version, upgrade))
        return True

    monkeypatch.setattr(ps_mod.plugin_service, "install_plugin", fake_install)
    mgr = ModuleManager()
    mgr.base_path = str(tmp_path)
    monkeypatch.setattr(mgr, "_ensure_plugin_state_schema", lambda: None)
    return mgr, Session, installs, plugin_dir


def _row(Session, module_id):
    with Session() as s:
        return s.get(PluginState, module_id)


def test_pin_does_not_rewrite_a_sibling_whose_id_contains_the_repo_name(env):
    mgr, Session, installs, plugin_dir = env
    with Session() as s:
        # Only the sibling has a row; the LIKE pattern %lyndrix_plugin_ai% matched it.
        s.add(PluginState(module_id="lyndrix.plugin.ai_assistant", is_active=True, installed_version="0.3.0",
                          desired_version="0.3.0", repo_url=ASSISTANT_URL, auto_update=False))
        s.add(PluginState(module_id="lyndrixXpluginXai", is_active=True, installed_version="9.9.9",
                          desired_version="9.9.9", repo_url="https://example.invalid/other/repo"))
        s.commit()

    asyncio.run(mgr._reconcile_desired_plugins_locked([{"url": AI_URL, "version": "1.2.0"}]))

    sibling = _row(Session, "lyndrix.plugin.ai_assistant")
    assert sibling.repo_url == ASSISTANT_URL and sibling.desired_version == "0.3.0"
    wildcard = _row(Session, "lyndrixXpluginXai")          # `_` is a LIKE wildcard
    assert wildcard.desired_version == "9.9.9"
    assert installs == [(AI_URL, "1.2.0", False)]          # folder missing → plain install


def test_pin_matches_the_row_by_normalised_repo_url(env):
    mgr, Session, installs, plugin_dir = env
    (plugin_dir / "lyndrix_plugin_ai").mkdir()
    with Session() as s:
        s.add(PluginState(module_id="lyndrix.plugin.ai", is_active=True, installed_version="1.2.0",
                          desired_version="1.1.0", repo_url=AI_URL.upper() + ".git/", auto_update=False))
        s.commit()

    asyncio.run(mgr._reconcile_desired_plugins_locked([{"url": AI_URL, "version": "v1.2.0"}]))

    row = _row(Session, "lyndrix.plugin.ai")
    assert row.desired_version == "v1.2.0" and row.repo_url == AI_URL
    assert installs == []                                   # already at the pinned version


def test_pin_matches_the_row_by_the_manifest_id_on_disk(env):
    """Row without a repo_url (manifest declares none): the folder's manifest id decides."""
    mgr, Session, installs, plugin_dir = env
    folder = plugin_dir / "lyndrix_plugin_ai"
    folder.mkdir()
    (folder / "entrypoint.py").write_text(
        'from core.api import ModuleManifest\n'
        'manifest = ModuleManifest(id="lyndrix.plugin.custom_id", name="AI", version="1.0.0", type="PLUGIN")\n'
    )
    with Session() as s:
        s.add(PluginState(module_id="lyndrix.plugin.custom_id", is_active=True, installed_version="1.0.0",
                          desired_version="1.0.0", repo_url=None, auto_update=False))
        s.commit()

    asyncio.run(mgr._reconcile_desired_plugins_locked([{"url": AI_URL, "version": "1.1.0"}]))

    row = _row(Session, "lyndrix.plugin.custom_id")
    assert row.desired_version == "1.1.0" and row.repo_url == AI_URL
    assert installs == [(AI_URL, "1.1.0", True)]            # pinned upgrade of the existing folder
