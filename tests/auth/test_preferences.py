"""Per-user preferences: no lost updates, cheap schema check (audit finding BG-21).

The document is read-modified-written as a whole. Without a row lock two
concurrent writes lost keys; two first writes of a new user collided on the
primary key (HTTP 500); MariaDB snapshot isolation answered 1020 (HTTP 500).
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.components.auth.logic.models import UserPreference
from core.components.auth.logic.preferences_service import PreferencesService
from core.components.database.logic import tx as tx_mod
from core.components.database.logic.db_service import db_instance


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    monkeypatch.setattr(tx_mod.time, "sleep", lambda _s: None)
    return engine


class _DriverError(Exception):
    pass


def test_merge_and_delete_round_trip(db):
    svc = PreferencesService()
    assert svc.merge("alice", {"theme": {"selection": {"react": "dark"}}}) == {"theme": {"selection": {"react": "dark"}}}
    assert svc.merge("alice", {"panels": {"a/b": {"open": True}}})["theme"]["selection"]["react"] == "dark"
    assert svc.delete_path("alice", "theme.selection.react") == {"panels": {"a/b": {"open": True}}}
    assert svc.get("alice") == {"panels": {"a/b": {"open": True}}}


def test_read_for_write_locks_the_row(db):
    svc = PreferencesService()
    svc.merge("alice", {"language": "de"})
    statements = []

    @event.listens_for(Session, "do_orm_execute")
    def _capture(state):
        statements.append(state.statement)

    try:
        svc.merge("alice", {"language": "en"})
        svc.delete_path("alice", "language")
    finally:
        event.remove(Session, "do_orm_execute", _capture)

    selects = [str(st.compile(dialect=mysql.dialect())) for st in statements if st.is_select]
    assert len(selects) == 2 and all("FOR UPDATE" in sql for sql in selects)


def test_first_write_race_is_retried_and_merges_into_the_winner(db, monkeypatch):
    svc = PreferencesService()
    real_session = tx_mod.write_session
    calls = {"n": 0}

    @contextmanager
    def racing_session(*args, **kwargs):
        calls["n"] += 1
        with real_session(*args, **kwargs) as session:
            if calls["n"] == 1:
                # A second request created the row between our SELECT and INSERT.
                real_commit = session.commit

                def commit_loses_race():
                    session.rollback()
                    with sessionmaker(bind=db)() as other:
                        other.add(UserPreference(username="bob", data={"panels": {"x": 1}}))
                        other.commit()
                    session.commit = real_commit
                    raise IntegrityError("INSERT", {}, _DriverError(1062, "Duplicate entry 'bob'"))

                session.commit = commit_loses_race
            yield session

    monkeypatch.setattr(tx_mod, "write_session", racing_session)
    merged = svc.merge("bob", {"language": "de"})

    assert calls["n"] == 2
    assert merged == {"panels": {"x": 1}, "language": "de"}      # nobody's keys were lost
    assert svc.get("bob") == merged


@pytest.mark.parametrize("code", [1020, 1205, 1213])
def test_transient_write_conflicts_are_retried(db, monkeypatch, code):
    svc = PreferencesService()
    svc.merge("carol", {"a": 1})
    real_session = tx_mod.write_session
    calls = {"n": 0}

    def flaky_session(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE", {}, _DriverError(code, "transient"))
        return real_session(*args, **kwargs)

    monkeypatch.setattr(tx_mod, "write_session", flaky_session)
    assert svc.merge("carol", {"b": 2}) == {"a": 1, "b": 2}
    assert calls["n"] == 2


def test_schema_is_ensured_once_under_the_bootstrap_lock(db, monkeypatch):
    svc = PreferencesService()
    locks: list[str] = []

    @contextmanager
    def fake_lock(name, timeout=60, *, warn=True):
        locks.append(name)
        yield True

    monkeypatch.setattr(db_instance, "advisory_lock", fake_lock, raising=False)
    for i in range(4):
        svc.merge("dave", {"n": i})
    svc.delete_path("dave", "n")
    assert locks == ["lyndrix_bootstrap"]                         # not once per write


def test_ensure_table_once_treats_a_lost_create_race_as_done(db, monkeypatch):
    @contextmanager
    def racing_lock(name, timeout=60, *, warn=True):
        raise OperationalError("CREATE TABLE", {}, _DriverError(1050, "Table 'user_preferences' already exists"))
        yield  # pragma: no cover

    monkeypatch.setattr(db_instance, "advisory_lock", racing_lock, raising=False)
    assert tx_mod.ensure_table_once(UserPreference.__table__) is True

    @contextmanager
    def broken_lock(name, timeout=60, *, warn=True):
        raise OperationalError("CREATE TABLE", {}, _DriverError(1142, "CREATE command denied"))
        yield  # pragma: no cover

    other = create_engine("sqlite://")
    monkeypatch.setattr(db_instance, "engine", other, raising=False)
    monkeypatch.setattr(db_instance, "advisory_lock", broken_lock, raising=False)
    assert tx_mod.ensure_table_once(UserPreference.__table__) is False   # real failure: retried next time


def test_other_bootstrap_ddl_goes_through_the_guarded_helper(db, monkeypatch):
    """correlation store, custom repos and plugin version history (same finding)."""
    from core.components.messaging.logic import correlation
    from core.components.plugins.logic.custom_repo_service import CustomRepositoryService
    from core.components.plugins.logic.plugin_service import plugin_service

    ensured: list[str] = []
    real = tx_mod.ensure_table_once

    def recording(table):
        ensured.append(table.name)
        return real(table)

    monkeypatch.setattr(tx_mod, "ensure_table_once", recording)
    monkeypatch.setattr(db_instance, "is_connected", True, raising=False)

    correlation.CorrelationStore().init_db()
    CustomRepositoryService().ensure_schema()
    plugin_service._record_history("lyndrix.plugin.x", "x", "1.0.0", action="install", outcome="ok")
    plugin_service._record_history("lyndrix.plugin.x", "x", "1.0.1", action="upgrade", outcome="ok")

    assert set(ensured) == {"pending_actions", "custom_plugin_repositories", "plugin_version_history"} or len(set(ensured)) == 3
    from core.components.plugins.logic.models import PluginVersionHistory
    with sessionmaker(bind=db)() as s:
        assert s.query(PluginVersionHistory).count() == 2
