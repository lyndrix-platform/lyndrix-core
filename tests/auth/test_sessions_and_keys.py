"""AZ-05 — tokens end with the account, the password and (for sessions) time.

Session tokens and API keys never expired, survived the deletion of their
owner (and came back to life for a later account of the same name), survived a
password change, and an SSO-only user could give themselves a local password.
"""
from datetime import datetime, timedelta

import pytest

from config import settings
from core.components.auth.logic import identity_link_service
from core.components.auth.logic.api_key_service import api_key_service, is_session_label
from core.components.auth.logic.models import User, UserApiKey, UserIdentity, UserPreference
from core.components.auth.logic.providers.base import AuthResult

SESSION_LABEL = "web-session · Firefox on Linux · 2026-11-14 10:00 UTC"


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _count(auth_db, model, **filters):
    with auth_db() as s:
        return s.query(model).filter_by(**filters).count()


# ── a key dies with its account ─────────────────────────────────────────────


def test_deleting_a_user_removes_keys_identities_and_preferences(client, auth_db, mint_key, bearer):
    admin = bearer(mint_key("admin"))
    token = bearer(mint_key("viewer", label=SESSION_LABEL))
    api_key = bearer(mint_key("viewer", label="ci"))
    client.put("/api/me/preferences", headers=token, json={"preferences": {"language": "de"}})
    with auth_db() as s:
        viewer_id = s.query(User).filter(User.username == "viewer").one().id
        s.add(UserIdentity(user_id=viewer_id, provider="oidc", provider_user_id="sub-viewer", matched_by="new"))
        s.commit()

    assert client.delete("/api/users/viewer", headers=admin).status_code == 200

    assert _count(auth_db, UserApiKey, username="viewer") == 0
    assert _count(auth_db, UserIdentity, user_id=viewer_id) == 0
    assert _count(auth_db, UserPreference, username="viewer") == 0
    for old in (token, api_key):
        assert client.get("/api/auth/me", headers=old).status_code == 401
        assert client.post("/api/events/ticket", headers=old).status_code == 401


def test_key_without_an_owner_is_rejected_and_never_inherited(client, auth_db, mint_key, bearer):
    """Rows an older core left behind when it deleted a user."""
    orphan = bearer(mint_key("ghost", label="left-behind"))            # no 'ghost' account exists
    assert client.get("/api/auth/me", headers=orphan).status_code == 401

    created = client.post("/api/users", headers=bearer(mint_key("admin")), json={
        "username": "ghost", "password": "dummy-ghost-pass-not-real", "groups": ["INT_ADMIN"]})
    assert created.status_code == 201
    assert _count(auth_db, UserApiKey, username="ghost") == 0           # purged on creation
    assert client.get("/api/users", headers=orphan).status_code == 401  # not the new admin's key


def test_sso_created_profile_does_not_inherit_old_keys(auth_db, mint_key):
    mint_key("boss", label="left-behind")
    linked = identity_link_service.resolve(AuthResult(
        username="boss", full_name="The Boss", email="boss@corp.invalid", roles=[], provider="oidc",
        provider_user_id="sub-boss", email_verified=True))
    assert linked.username == "boss" and _count(auth_db, UserApiKey, username="boss") == 0


# ── a password change ends the other sessions ───────────────────────────────


def test_password_change_signs_out_other_sessions_but_keeps_api_keys(client, auth_db, mint_key, bearer, passwords):
    here = _login(client, "viewer", passwords["viewer"])
    elsewhere = _login(client, "viewer", passwords["viewer"])
    api_key = bearer(mint_key("viewer", label="ci"))

    r = client.post("/api/users/viewer/password", headers=here, json={
        "current_password": passwords["viewer"], "new_password": "dummy-new-pass-not-real"})
    assert r.status_code == 200 and r.json()["revoked_sessions"] == 1

    assert client.get("/api/auth/me", headers=here).status_code == 200        # the session that changed it
    assert client.get("/api/auth/me", headers=elsewhere).status_code == 401   # e.g. the thief's
    assert client.get("/api/auth/me", headers=api_key).status_code == 200     # named keys are kept


def test_password_change_via_patch_and_admin_reset_revoke_too(client, auth_db, mint_key, bearer, passwords):
    here = _login(client, "viewer", passwords["viewer"])
    elsewhere = _login(client, "viewer", passwords["viewer"])
    r = client.patch("/api/users/viewer", headers=here, json={
        "password": "dummy-new-pass-not-real", "current_password": passwords["viewer"]})
    assert r.status_code == 200 and r.json()["revoked_sessions"] == 1
    assert client.get("/api/auth/me", headers=elsewhere).status_code == 401

    # An administrator resets it: every session of the account ends.
    r = client.patch("/api/users/viewer", headers=bearer(mint_key("admin")),
                     json={"password": "dummy-reset-pass-not-real"})
    assert r.status_code == 200 and r.json()["revoked_sessions"] == 1
    assert client.get("/api/auth/me", headers=here).status_code == 401
    # A profile edit without a password touches no session.
    again = _login(client, "viewer", "dummy-reset-pass-not-real")
    assert "revoked_sessions" not in client.patch("/api/users/viewer", headers=again,
                                                  json={"full_name": "Vera"}).json()
    assert client.get("/api/auth/me", headers=again).status_code == 200


# ── accounts without a local password ───────────────────────────────────────


@pytest.fixture()
def sso_user(auth_db, mint_key, bearer):
    with auth_db() as s:
        s.add(User(username="ssouser", hashed_password=None, roles=[], groups=["INT_VIEWER"],
                   extra_permissions=[]))
        s.commit()
    return bearer(mint_key("ssouser", label=SESSION_LABEL))


def test_sso_only_account_cannot_give_itself_a_local_password(client, auth_db, sso_user):
    patch = client.patch("/api/users/ssouser", headers=sso_user, json={
        "password": "dummy-local-bypass-not-real", "current_password": "anything"})
    post = client.post("/api/users/ssouser/password", headers=sso_user, json={
        "current_password": "anything", "new_password": "dummy-local-bypass-not-real"})
    assert patch.status_code == post.status_code == 403
    assert "external provider" in patch.json()["detail"]
    with auth_db() as s:
        assert s.query(User).filter(User.username == "ssouser").one().hashed_password is None
    # The rest of the profile stays editable.
    assert client.patch("/api/users/ssouser", headers=sso_user, json={"full_name": "S. User"}).status_code == 200


def test_an_administrator_can_still_set_one(client, auth_db, sso_user, mint_key, bearer):
    r = client.patch("/api/users/ssouser", headers=bearer(mint_key("admin")),
                     json={"password": "dummy-admin-set-pass-not-real"})
    assert r.status_code == 200
    assert client.post("/api/auth/login", json={
        "username": "ssouser", "password": "dummy-admin-set-pass-not-real"}).status_code == 200


# ── login sessions expire, API keys do not ──────────────────────────────────


def _age(auth_db, label, hours):
    with auth_db() as s:
        key = s.query(UserApiKey).filter(UserApiKey.label == label).one()
        key.created_at = datetime.utcnow() - timedelta(hours=hours)
        s.commit()


def test_login_session_expires_after_the_maximum_age(client, auth_db, mint_key, bearer):
    assert settings.LYNDRIX_SESSION_MAX_AGE_HOURS == 720                 # 30 days by default
    session = bearer(mint_key("viewer", label=SESSION_LABEL))
    api_key = bearer(mint_key("viewer", label="ci"))
    _age(auth_db, SESSION_LABEL, 719)
    assert client.get("/api/auth/me", headers=session).status_code == 200

    _age(auth_db, SESSION_LABEL, 721)
    _age(auth_db, "ci", 9000)
    assert client.get("/api/auth/me", headers=session).status_code == 401  # → the shell goes to /login
    assert client.get("/api/auth/me", headers=api_key).status_code == 200  # named keys never expire
    assert _count(auth_db, UserApiKey, label=SESSION_LABEL) == 0           # the expired row is removed


def test_session_expiry_can_be_switched_off(client, auth_db, mint_key, bearer, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_SESSION_MAX_AGE_HOURS", 0)
    session = bearer(mint_key("viewer", label=SESSION_LABEL))
    _age(auth_db, SESSION_LABEL, 100000)
    assert client.get("/api/auth/me", headers=session).status_code == 200


def test_a_login_mints_a_session_and_the_prefix_is_reserved(client, auth_db, passwords, mint_key, bearer):
    headers = _login(client, "viewer", passwords["viewer"])
    with auth_db() as s:
        label = s.query(UserApiKey).filter(UserApiKey.username == "viewer").one().label
    assert is_session_label(label)
    assert api_key_service.verify(headers["Authorization"].split()[1]).is_session is True

    # A user-created key cannot pose as a session (or the other way round).
    for label in ("web-session mine", "Web-Session · x"):
        r = client.post("/api/users/viewer/api-keys", headers=headers, json={"label": label})
        assert r.status_code == 422 and "reserved" in r.json()["detail"]
    ok = client.post("/api/users/viewer/api-keys", headers=headers, json={"label": "my automation"})
    assert ok.status_code == 201
    assert api_key_service.verify(ok.json()["token"]).is_session is False
