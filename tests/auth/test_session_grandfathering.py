"""R1 — the session max-age must not end the sessions an upgrade finds.

Core 1.12 gave login sessions an absolute lifetime (30 days). The first request
an upgraded instance saw for an older session DELETED the row — in the shared
database, so the token also died on the instances still running the previous
version: operator scripts holding a session token broke in the middle of the
rollout and every user with an old session was signed out.

Now the deployment records once when the rule began to apply; sessions created
before that are "legacy" and ``LYNDRIX_SESSION_MAX_AGE_LEGACY`` decides.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from config import Settings, settings
from core.components.auth.logic import session_policy
from core.components.auth.logic.api_key_service import api_key_service
from core.components.auth.logic.auth_service import auth_service
from core.components.auth.logic.models import AuthMarker, UserApiKey
from core.components.database.logic.db_service import Base, db_instance

LEGACY = "web-session · Firefox on Linux · 2026-10-15 08:00 UTC"
FRESH = "web-session · Firefox on Linux · 2026-11-15 08:00 UTC"
DAY = 24


def _age(auth_db, label, hours):
    with auth_db() as s:
        key = s.query(UserApiKey).filter(UserApiKey.label == label).one()
        key.created_at = datetime.utcnow() - timedelta(hours=hours)
        s.commit()


def _rows(auth_db, label):
    with auth_db() as s:
        return s.query(UserApiKey).filter(UserApiKey.label == label).count()


def _me(client, headers):
    return client.get("/api/auth/me", headers=headers).status_code


@pytest.fixture()
def upgraded(client, auth_db, mint_key, bearer, enforced_since):
    """A deployment upgraded 40 days ago that holds a session from 31 days
    before the upgrade and one created 31 days after it."""
    legacy = bearer(mint_key("admin", label=LEGACY))
    fresh = bearer(mint_key("viewer", label=FRESH))
    enforced_since(40 * DAY)
    _age(auth_db, LEGACY, 71 * DAY)
    _age(auth_db, FRESH, 9 * DAY)
    return legacy, fresh


# ── default: legacy sessions are exempt, new ones expire ────────────────────


def test_a_session_from_before_the_upgrade_survives_it(client, auth_db, upgraded):
    legacy, fresh = upgraded
    assert settings.LYNDRIX_SESSION_MAX_AGE_LEGACY == "exempt"
    assert _me(client, legacy) == 200
    assert _me(client, legacy) == 200
    assert _rows(auth_db, LEGACY) == 1               # the row other instances share is untouched
    assert _me(client, fresh) == 200                 # 9 days old


def test_a_session_created_after_the_upgrade_still_expires(client, auth_db, upgraded):
    legacy, fresh = upgraded
    _age(auth_db, FRESH, 31 * DAY)                    # created after the instant, older than 30 days
    assert _me(client, fresh) == 401
    assert _rows(auth_db, FRESH) == 0
    assert _me(client, legacy) == 200


def test_upgrade_day_nothing_is_older_than_the_rule(client, auth_db, mint_key, bearer):
    """The rollout itself: the instant was recorded seconds ago."""
    operator = bearer(mint_key("admin", label=LEGACY))
    _age(auth_db, LEGACY, 31 * DAY)
    assert _me(client, operator) == 200 and _rows(auth_db, LEGACY) == 1


# ── the operator's choices ──────────────────────────────────────────────────


def test_from_enforcement_counts_the_age_from_the_upgrade(client, auth_db, upgraded, enforced_since, monkeypatch):
    legacy, _fresh = upgraded
    monkeypatch.setattr(settings, "LYNDRIX_SESSION_MAX_AGE_LEGACY", "from_enforcement")
    enforced_since(29 * DAY)                          # 29 days after the upgrade: still valid
    assert _me(client, legacy) == 200
    enforced_since(31 * DAY)                          # 31 days after it: all legacy sessions end
    assert _me(client, legacy) == 401
    assert _rows(auth_db, LEGACY) == 0


def test_strict_treats_legacy_sessions_like_any_other(client, auth_db, upgraded, monkeypatch):
    legacy, fresh = upgraded
    monkeypatch.setattr(settings, "LYNDRIX_SESSION_MAX_AGE_LEGACY", "strict")
    assert _me(client, legacy) == 401 and _rows(auth_db, LEGACY) == 0
    assert _me(client, fresh) == 200


def test_max_age_zero_still_switches_expiry_off_for_everyone(client, auth_db, upgraded, monkeypatch):
    legacy, fresh = upgraded
    monkeypatch.setattr(settings, "LYNDRIX_SESSION_MAX_AGE_HOURS", 0)
    monkeypatch.setattr(settings, "LYNDRIX_SESSION_MAX_AGE_LEGACY", "strict")
    _age(auth_db, FRESH, 400 * DAY)
    assert _me(client, legacy) == 200 and _me(client, fresh) == 200


def test_the_setting_rejects_unknown_values():
    assert Settings(LYNDRIX_SESSION_MAX_AGE_LEGACY=" Strict ").LYNDRIX_SESSION_MAX_AGE_LEGACY == "strict"
    with pytest.raises(ValueError, match="LYNDRIX_SESSION_MAX_AGE_LEGACY"):
        Settings(LYNDRIX_SESSION_MAX_AGE_LEGACY="forever")


# ── exempt from the AGE only ────────────────────────────────────────────────


def test_a_legacy_session_can_be_revoked(client, auth_db, upgraded):
    legacy, _fresh = upgraded
    with auth_db() as s:
        key_id = s.query(UserApiKey).filter(UserApiKey.label == LEGACY).one().id
    assert client.delete(f"/api/users/admin/api-keys/{key_id}", headers=legacy).status_code == 200
    assert _me(client, legacy) == 401


def test_a_password_change_ends_legacy_sessions(client, auth_db, upgraded, passwords):
    legacy, _fresh = upgraded
    here = {"Authorization": "Bearer " + client.post("/api/auth/login", json={
        "username": "admin", "password": passwords["admin"]}).json()["token"]}
    r = client.post("/api/users/admin/password", headers=here, json={
        "current_password": passwords["admin"], "new_password": "dummy-new-pass-not-real"})
    assert r.status_code == 200 and r.json()["revoked_sessions"] == 1
    assert _me(client, legacy) == 401


def test_deleting_the_account_ends_its_legacy_sessions(client, auth_db, mint_key, bearer, enforced_since):
    legacy = bearer(mint_key("viewer", label=LEGACY))
    enforced_since(40 * DAY)
    _age(auth_db, LEGACY, 71 * DAY)
    assert _me(client, legacy) == 200
    assert client.delete("/api/users/viewer", headers=bearer(mint_key("admin"))).status_code == 200
    assert _me(client, legacy) == 401 and _rows(auth_db, LEGACY) == 0


# ── an administrator can see it ─────────────────────────────────────────────


def test_the_key_listing_shows_which_sessions_do_not_expire(
        client, auth_db, upgraded, mint_key, bearer, monkeypatch, enforced_since):
    legacy, _fresh = upgraded
    mint_key("admin", label="ci")
    mint_key("admin", label=FRESH + " (2)")

    def listing():
        keys = client.get("/api/users/admin/api-keys", headers=legacy).json()["api_keys"]
        return {k["label"]: k for k in keys}

    keys = listing()
    assert keys[LEGACY]["is_session"] is True
    assert keys[LEGACY]["expiry"] == "legacy_exempt" and keys[LEGACY]["expires_at"] is None
    new = keys[FRESH + " (2)"]
    assert new["expiry"] == "max_age"
    assert datetime.fromisoformat(new["expires_at"]) - datetime.fromisoformat(new["created_at"]) == timedelta(days=30)
    assert keys["ci"]["is_session"] is False and keys["ci"]["expiry"] is None and keys["ci"]["expires_at"] is None

    monkeypatch.setattr(settings, "LYNDRIX_SESSION_MAX_AGE_LEGACY", "from_enforcement")
    enforced_since(20 * DAY)
    again = listing()[LEGACY]
    assert again["expiry"] == "legacy_from_enforcement"
    remaining = datetime.fromisoformat(again["expires_at"]) - datetime.utcnow()
    assert timedelta(days=9) < remaining < timedelta(days=11)


# ── fail safe: nothing expires while the instant is unknown ─────────────────


def test_no_recorded_instant_means_nothing_is_deleted(client, auth_db, mint_key, bearer, enforced_since):
    """Requests can arrive before the IAM bootstrap wrote the record."""
    old = bearer(mint_key("admin", label=LEGACY))
    _age(auth_db, LEGACY, 400 * DAY)
    enforced_since(None)
    assert _me(client, old) == 200 and _rows(auth_db, LEGACY) == 1
    assert session_policy.lifetime(datetime.utcnow() - timedelta(days=400)).reason == "unknown"

    # Once the bootstrap ran, the session is a legacy one — still there.
    session_policy.reset_for_tests()
    assert session_policy.ensure_enforcement_start() is not None
    assert _me(client, old) == 200 and _rows(auth_db, LEGACY) == 1


def test_a_missing_table_means_nothing_is_deleted(client, auth_db, mint_key, bearer):
    """An instance whose bootstrap has not created ``auth_markers`` yet."""
    old = bearer(mint_key("admin", label=LEGACY))
    _age(auth_db, LEGACY, 400 * DAY)
    AuthMarker.__table__.drop(bind=db_instance.engine)
    session_policy.reset_for_tests()
    assert _me(client, old) == 200
    assert _me(client, old) == 200
    assert _rows(auth_db, LEGACY) == 1


def test_database_not_connected_is_unknown_not_expired(monkeypatch):
    session_policy.reset_for_tests()
    monkeypatch.setattr(db_instance, "SessionLocal", None, raising=False)
    monkeypatch.setattr(db_instance, "engine", None, raising=False)
    life = session_policy.lifetime(datetime.utcnow() - timedelta(days=400))
    assert life.expires_at is None and life.reason == "unknown" and not life.expired()
    assert session_policy.ensure_enforcement_start() is None


def test_strict_needs_no_recorded_instant(client, auth_db, mint_key, bearer, enforced_since, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_SESSION_MAX_AGE_LEGACY", "strict")
    old = bearer(mint_key("admin", label=LEGACY))
    _age(auth_db, LEGACY, 31 * DAY)
    enforced_since(None)
    assert _me(client, old) == 401


# ── recording the instant: once, cluster-wide ───────────────────────────────


def test_the_instant_is_recorded_once_and_never_moves(auth_db, enforced_since):
    enforced_since(40 * DAY)
    first = session_policy.ensure_enforcement_start()
    session_policy.reset_for_tests()                  # "another instance" / a later boot
    assert session_policy.ensure_enforcement_start() == first
    assert datetime.utcnow() - first > timedelta(days=39)
    with auth_db() as s:
        assert s.query(AuthMarker).count() == 1


@pytest.fixture()
def shared_db(tmp_path, monkeypatch):
    """A database two "instances" reach over separate connections."""
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    mine, peer = create_engine(url), create_engine(url)
    AuthMarker.__table__.create(bind=mine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=mine)
    monkeypatch.setattr(db_instance, "engine", mine, raising=False)
    monkeypatch.setattr(db_instance, "SessionLocal", factory, raising=False)
    session_policy.reset_for_tests()
    yield factory, sessionmaker(bind=peer)
    session_policy.reset_for_tests()
    mine.dispose()
    peer.dispose()


def test_two_instances_recording_at_once_agree_on_one_instant(shared_db):
    """The peer's INSERT commits between our "is there a row?" and our INSERT."""
    factory, peer_factory = shared_db
    peer_instant = datetime(2026, 11, 15, 9, 0, 0)

    @event.listens_for(factory, "before_flush")
    def _peer_wins(_session, _ctx, _instances):
        with peer_factory() as p:
            if p.get(AuthMarker, session_policy.MARKER_NAME) is None:
                p.add(AuthMarker(name=session_policy.MARKER_NAME, at=peer_instant))
                p.commit()

    assert session_policy.ensure_enforcement_start() == peer_instant   # lost the race, uses the peer's
    assert session_policy.enforcement_start() == peer_instant
    with factory() as s:
        assert s.query(AuthMarker).count() == 1


def test_the_iam_bootstrap_records_the_instant(shared_db, monkeypatch):
    for step in ("_migrate_schema", "_seed_default_groups", "_ensure_admin_permissions",
                 "_seed_users", "_initialize_providers"):
        monkeypatch.setattr(auth_service, step, lambda: None)
    AuthMarker.__table__.drop(bind=db_instance.engine)   # a database from the previous version
    monkeypatch.setattr(Base.metadata, "create_all", lambda bind: AuthMarker.__table__.create(bind=bind))

    before = datetime.utcnow()
    auth_service._bootstrap_iam()
    session_policy.reset_for_tests()
    assert before <= session_policy.enforcement_start() <= datetime.utcnow()
