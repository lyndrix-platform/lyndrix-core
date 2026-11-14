"""AZ-04 — the scopes of an API key are a real boundary.

A key limited to e.g. ``plugin:<id>:api:read`` could mint an unscoped key or
set a new password (self-service routes never looked at scopes), and a scoped
key of a superadmin ignored its scopes altogether.
"""
import pytest

from core.api import events_api
from core.api.security import ApiIdentity
from core.components.auth.logic.models import UserApiKey
from core.components.plugins.api.nav_api import _visible_plugins

PLUGIN_SCOPE = "plugin:lyndrix.plugin.x:api:read"


@pytest.fixture()
def scoped(mint_key, bearer):
    return bearer(mint_key("viewer", label="ci-scoped", scopes=[PLUGIN_SCOPE]))


@pytest.fixture()
def session(mint_key, bearer):
    return bearer(mint_key("viewer", label="web-session · test"))


# ── ApiIdentity.allows ──────────────────────────────────────────────────────


def test_scopes_are_applied_before_the_superadmin_bypass():
    key = ApiIdentity(method="user_api_key", username="root", roles=["superadmin"],
                      permissions=frozenset(), key_scopes=["api:read"])
    assert key.allows("api:read") and key.allows("plugin:lyndrix.plugin.x:api:read")
    assert not key.allows("admin:read") and not key.allows("api:write")
    assert key.is_scoped and not key.is_superadmin

    unscoped = ApiIdentity(method="user_api_key", username="root", roles=["superadmin"],
                           permissions=frozenset())
    assert unscoped.allows("admin:write") and unscoped.is_superadmin and not unscoped.is_scoped


def test_scopes_never_widen_the_owners_permissions():
    key = ApiIdentity(method="user_api_key", username="v", permissions=frozenset({"api:read"}),
                      key_scopes=["api:read", "admin:write"])
    assert key.allows("api:read") and not key.allows("admin:write")


def test_the_system_key_has_no_scopes():
    system = ApiIdentity(method="api_key", is_system=True, roles=["admin", "superadmin"])
    assert system.allows("anything:at:all") and system.is_superadmin


# ── credential management needs a full credential ───────────────────────────


def test_scoped_key_cannot_mint_keys_or_change_credentials(client, auth_db, scoped, passwords):
    assert client.get("/api/plugins", headers=scoped).status_code == 403          # outside its scope
    r = client.post("/api/users/viewer/api-keys", headers=scoped, json={"label": "escalated", "scopes": []})
    assert r.status_code == 403 and "scope-restricted" in r.json()["detail"]
    assert client.patch("/api/users/viewer", headers=scoped, json={
        "password": "dummy-pwned-pass-not-real", "current_password": passwords["viewer"]}).status_code == 403
    assert client.patch("/api/users/viewer", headers=scoped,
                        json={"email": "boss@corp.invalid"}).status_code == 403
    assert client.post("/api/users/viewer/password", headers=scoped, json={
        "current_password": passwords["viewer"], "new_password": "dummy-pwned-pass-not-real"}).status_code == 403
    with auth_db() as s:
        assert s.query(UserApiKey).filter(UserApiKey.username == "viewer").count() == 1


def test_superadmin_key_with_scopes_is_bounded_by_them(client, mint_key, bearer):
    read_only = bearer(mint_key("breakglass", label="ro", scopes=["api:read"]))
    assert client.get("/api/nav", headers=read_only).status_code == 200            # api:read
    assert client.get("/api/users", headers=read_only).status_code == 403          # admin:read
    assert client.post("/api/permissions/groups", headers=read_only,
                       json={"name": "scope-test", "permissions": []}).status_code == 403
    # ...and it is no superadmin for the tier checks either.
    assert client.post("/api/users/breakglass/api-keys", headers=read_only,
                       json={"label": "x"}).status_code == 403


def test_scoped_key_may_retire_itself_but_nothing_else(client, auth_db, mint_key, bearer, scoped, session):
    with auth_db() as s:
        keys = {k.label: k.id for k in s.query(UserApiKey).filter(UserApiKey.username == "viewer")}
    other = keys["web-session · test"]
    assert client.delete(f"/api/users/viewer/api-keys/{other}", headers=scoped).status_code == 403
    assert client.delete(f"/api/users/viewer/api-keys/{keys['ci-scoped']}", headers=scoped).status_code == 200
    assert client.get("/api/auth/me", headers=scoped).status_code == 401           # gone
    assert client.get("/api/auth/me", headers=session).status_code == 200


def test_sessions_keep_full_self_service(client, session, passwords):
    minted = client.post("/api/users/viewer/api-keys", headers=session,
                         json={"label": "my key", "scopes": [PLUGIN_SCOPE]})
    assert minted.status_code == 201 and minted.json()["api_key"]["scopes"] == [PLUGIN_SCOPE]
    assert client.patch("/api/users/viewer", headers=session,
                        json={"full_name": "Vera", "email": "vera@corp.invalid"}).status_code == 200


# ── own password through PATCH needs the current one ────────────────────────


def test_own_password_via_patch_needs_the_current_password(client, session, passwords, fast_argon2):
    new = {"password": "dummy-new-pass-not-real"}
    missing = client.patch("/api/users/viewer", headers=session, json=new)
    assert missing.status_code == 422 and "current password" in missing.json()["detail"]

    wrong = client.patch("/api/users/viewer", headers=session, json={**new, "current_password": "nope"})
    assert wrong.status_code == 400                    # not 401 — that would log the browser out
    assert client.get("/api/auth/me", headers=session).status_code == 200

    codes = [client.patch("/api/users/viewer", headers=session,
                          json={**new, "current_password": f"guess-{i}"}).status_code for i in range(5)]
    assert codes == [400] * 4 + [429]                  # throttled like any password check


def test_own_password_via_patch_with_the_current_password_works(client, session, passwords):
    ok = client.patch("/api/users/viewer", headers=session,
                      json={"password": "dummy-new-pass-not-real", "current_password": passwords["viewer"]})
    assert ok.status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "viewer", "password": "dummy-new-pass-not-real"}).status_code == 200


def test_admin_reset_of_another_account_needs_no_current_password(client, mint_key, bearer):
    r = client.patch("/api/users/viewer", headers=bearer(mint_key("admin")),
                     json={"password": "dummy-reset-pass-not-real"})
    assert r.status_code == 200


# ── views derived from the credential ───────────────────────────────────────


def test_me_access_reports_the_scoped_view(client, mint_key, bearer):
    scoped_root = bearer(mint_key("breakglass", label="ro", scopes=["api:read"]))
    view = client.get("/api/me/access", headers=scoped_root).json()
    assert view["is_superadmin"] is False

    scoped_admin = bearer(mint_key("admin", label="ro", scopes=["api:read"]))
    assert client.get("/api/me/access", headers=scoped_admin).json()["permissions"] == ["api:read"]
    full = client.get("/api/me/access", headers=bearer(mint_key("admin"))).json()
    assert "admin:write" in full["permissions"]


def test_nav_visibility_honours_scopes():
    owner = frozenset({"api:read", "plugin:p1", "plugin:p2"})
    full = ApiIdentity(method="user_api_key", username="u", permissions=owner)
    scoped = ApiIdentity(method="user_api_key", username="u", permissions=owner,
                         key_scopes=["api:read", "plugin:p1"])
    scoped_root = ApiIdentity(method="user_api_key", username="root", roles=["superadmin"],
                              permissions=frozenset(), key_scopes=["api:read"])
    assert _visible_plugins(full, ["p1", "p2", "p3"]) == ["p1", "p2"]
    assert _visible_plugins(scoped, ["p1", "p2", "p3"]) == ["p1"]
    assert _visible_plugins(scoped_root, ["p1", "p2"]) == []


def test_event_stream_ticket_honours_scopes(client, mint_key, bearer, scoped, session):
    assert client.post("/api/events/ticket", headers=scoped).status_code == 403
    assert client.post("/api/events/ticket", headers=session).status_code == 200
    read_scoped = bearer(mint_key("viewer", label="reader", scopes=["api:read"]))
    assert client.post("/api/events/ticket", headers=read_scoped).status_code == 200


def test_header_authenticated_stream_of_a_scoped_key_is_public_only():
    narrow = ApiIdentity(method="user_api_key", username="v", permissions=frozenset({"api:read"}),
                         key_scopes=[PLUGIN_SCOPE])
    reader = ApiIdentity(method="user_api_key", username="v", permissions=frozenset({"api:read"}),
                         key_scopes=["api:read"])
    session_identity = ApiIdentity(method="user_api_key", username="v", permissions=frozenset())
    assert events_api._may_join_authenticated_stream(narrow) is False
    assert events_api._may_join_authenticated_stream(reader) is True
    assert events_api._may_join_authenticated_stream(session_identity) is True
    assert events_api._may_join_authenticated_stream(None) is False
