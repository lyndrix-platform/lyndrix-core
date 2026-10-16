"""The update badge (latest_version / update_available in /api/plugins) comes
from plugin_states.latest_version, written by the leader's check — every
instance answers the same (core 1.8.1)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.components.database.logic import db_service
from core.components.plugins.logic.models import PluginState
from core.components.plugins.logic.plugin_service import PluginService


def _db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginState.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    with Session() as s:
        s.add(PluginState(module_id="lyndrix.plugin.x", is_active=True, installed_version="1.2.0",
                          desired_version="1.2.0", repo_url="https://x/y"))
        s.commit()
    return Session


def test_badge_follows_the_stored_latest_version(monkeypatch):
    Session = _db(monkeypatch)
    svc = PluginService.__new__(PluginService)
    svc._latest_known, svc._latest_db, svc._latest_db_at = {}, {}, 0.0

    assert svc.latest_version_info("lyndrix.plugin.x", "1.2.0") == (None, False)   # nothing checked yet

    svc._store_latest({"lyndrix.plugin.x": "v1.3.0"})                             # the leader's check
    with Session() as s:
        row = s.get(PluginState, "lyndrix.plugin.x")
        assert row.latest_version == "v1.3.0" and row.latest_checked_at is not None

    follower = PluginService.__new__(PluginService)                                 # another instance
    follower._latest_known, follower._latest_db, follower._latest_db_at = {}, {}, 0.0
    assert follower.latest_version_info("lyndrix.plugin.x", "1.2.0") == ("v1.3.0", True)
    assert follower.latest_version_info("lyndrix.plugin.x", "1.3.0") == ("v1.3.0", False)

    follower.note_latest("lyndrix.plugin.x", "v1.4.0")                             # bridged announcement
    assert follower.latest_version_info("lyndrix.plugin.x", "1.3.0") == ("v1.4.0", True)
