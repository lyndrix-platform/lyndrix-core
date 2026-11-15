"""AZ-03 — identity linking fails CLOSED.

A login whose external identity cannot be bound to a local profile must be
denied. Before the fix ``resolve()`` handed back the provider's raw result on
any error, and every caller minted a session for that EXTERNAL username — a
same-named local account (``admin``) was taken over.
"""
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy.exc import IntegrityError

from core.components.auth.logic import identity_link_service
from core.components.auth.logic.identity_link_service import IdentityLinkError
from core.components.auth.logic.models import User, UserApiKey, UserIdentity
from core.components.auth.logic.providers.base import AuthProvider, AuthResult
from core.components.auth.logic.providers.registry import provider_registry


def _external(username="admin", sub="idp-sub-mallory", provider="oidc", **kw) -> AuthResult:
    defaults = dict(full_name="Mallory", email="mallory@ext.invalid", roles=["user"],
                    provider=provider, provider_user_id=sub, email_verified=False)
    defaults.update(kw)
    return AuthResult(username=username, **defaults)


class FakeOIDC(AuthProvider):
    """Stands in for the IdP: an external account whose username is ``admin``."""

    provider_id = "oidc"
    display_name = "SSO"

    def __init__(self, sub="idp-sub-mallory"):
        self.sub = sub

    def is_configured(self):
        return True

    def is_sso(self):
        return True

    async def authenticate(self, username, password):
        return None

    async def state_meta_async(self, state):
        return {"flow": "react", "redirect": "http://localhost:5173/auth/callback|/"}

    async def exchange_code(self, code, state):
        return _external(sub=self.sub)

    def consume_result(self, state):
        return None


@pytest.fixture()
def fake_oidc(auth_db):
    provider = FakeOIDC()
    previous = dict(provider_registry._providers)
    provider_registry.register(provider)
    yield provider
    provider_registry._providers.clear()
    provider_registry._providers.update(previous)


def _sso_login(client):
    """Run callback + token exchange; returns (callback response, exchange response or None)."""
    r = client.get("/auth/callback/oidc", params={"code": "c", "state": "s"}, follow_redirects=False)
    code = parse_qs(urlsplit(r.headers.get("location", "")).query).get("code", [""])[0]
    if not code:
        return r, None
    return r, client.post("/api/auth/token-exchange", json={"code": code})


# ── resolve() ───────────────────────────────────────────────────────────────


def test_resolve_raises_instead_of_returning_the_unlinked_result(auth_db, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(identity_link_service, "_match_or_create", boom)
    with pytest.raises(IdentityLinkError):
        identity_link_service.resolve(_external())
    with auth_db() as s:
        assert s.query(UserIdentity).count() == 0


def test_resolve_raises_without_a_database(monkeypatch):
    from core.components.database.logic.db_service import db_instance

    monkeypatch.setattr(db_instance, "SessionLocal", None, raising=False)
    with pytest.raises(IdentityLinkError):
        identity_link_service.resolve(_external())


def test_username_collision_never_resolves_to_the_local_account(auth_db):
    linked = identity_link_service.resolve(_external())
    assert linked.username == "admin_oidc"
    assert linked.linked_user_id
    with auth_db() as s:
        assert s.query(User).filter(User.id == linked.linked_user_id).one().username == "admin_oidc"


def test_lost_first_login_race_ends_on_the_winners_profile(auth_db, monkeypatch):
    """Two first logins of the same external account: the loser's insert hits the
    unique username, the retry finds the winner's identity — ONE profile, and
    the loser logs in as that profile (not as the raw external username)."""
    real_unique = identity_link_service._unique_username
    calls = {"n": 0}

    def stale_unique(session, wanted, provider):
        calls["n"] += 1
        if calls["n"] == 1:
            # The concurrent winner commits its profile + identity now; our
            # transaction read "free" before and still inserts the same name.
            with auth_db() as other:
                winner = User(username="admin_oidc", full_name="Mallory", hashed_password=None,
                              roles=[], groups=[], extra_permissions=[])
                other.add(winner)
                other.flush()
                other.add(UserIdentity(user_id=winner.id, provider="oidc",
                                       provider_user_id="idp-sub-mallory", matched_by="new"))
                other.commit()
            return "admin_oidc"
        return real_unique(session, wanted, provider)

    monkeypatch.setattr(identity_link_service, "_unique_username", stale_unique)
    linked = identity_link_service.resolve(_external())

    assert linked.username == "admin_oidc"
    with auth_db() as s:
        assert s.query(User).filter(User.username.like("admin%")).count() == 2  # admin + admin_oidc
        assert s.query(UserIdentity).filter(UserIdentity.provider == "oidc").count() == 1


def test_persistent_constraint_error_denies_the_login(auth_db, monkeypatch):
    def always_duplicate(*_a, **_kw):
        raise IntegrityError("INSERT", {}, Exception("duplicate"))

    monkeypatch.setattr(identity_link_service, "_resolve_once", always_duplicate)
    with pytest.raises(IdentityLinkError):
        identity_link_service.resolve(_external())


def test_overlong_claims_are_fitted_before_the_insert(auth_db):
    long_email = "a" * 120 + "@ext.invalid"
    linked = identity_link_service.resolve(_external(
        username="x" * 80, sub="s" * 400, full_name="N" * 300, email=long_email,
    ))
    with auth_db() as s:
        user = s.query(User).filter(User.id == linked.linked_user_id).one()
        identity = s.query(UserIdentity).filter(UserIdentity.user_id == user.id).one()
    assert len(user.username) <= 50 and len(user.full_name) == 100
    assert user.email is None                      # dropped, not truncated to another address
    assert len(identity.provider_user_id) <= 255 and identity.provider_user_id.startswith("sha256:")
    assert len(identity.email_at_link) == 100
    # The hashed id is stable: the next login finds the same profile.
    again = identity_link_service.resolve(_external(
        username="x" * 80, sub="s" * 400, full_name="N" * 300, email=long_email,
    ))
    assert again.linked_user_id == linked.linked_user_id


def test_username_variants_always_fit_the_column(auth_db):
    with auth_db() as s:
        base = "u" * 40
        s.add(User(username=base, hashed_password=None, roles=[], groups=[], extra_permissions=[]))
        s.add(User(username=f"{base}_customid", hashed_password=None, roles=[], groups=[],
                   extra_permissions=[]))
        s.commit()
        name = identity_link_service._unique_username(s, "u" * 60, "customidprovider")
    assert len(name) <= 50 and name not in (base, f"{base}_customid")


def test_orphaned_identity_is_relinked_in_one_flush(auth_db):
    """A stale identity row (its user is gone) is re-pointed, not duplicated."""
    with auth_db() as s:
        s.add(UserIdentity(user_id="gone", provider="oidc", provider_user_id="idp-sub-mallory",
                           matched_by="new"))
        s.commit()
    linked = identity_link_service.resolve(_external())
    with auth_db() as s:
        rows = s.query(UserIdentity).filter(UserIdentity.provider == "oidc").all()
    assert len(rows) == 1 and rows[0].user_id == linked.linked_user_id


# ── callers deny the login ──────────────────────────────────────────────────


def test_password_login_is_denied_when_linking_fails(client, auth_db, passwords, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("simulated database failure")

    # The first login of 'admin' creates its local identity row via _match_or_create.
    monkeypatch.setattr(identity_link_service, "_match_or_create", boom)
    r = client.post("/api/auth/login", json={"username": "admin", "password": passwords["admin"]})

    assert r.status_code == 503 and "token" not in r.json()
    assert r.headers.get("retry-after")
    with auth_db() as s:
        assert s.query(UserApiKey).count() == 0          # no session was minted


def test_password_login_still_works(client, auth_db, passwords):
    r = client.post("/api/auth/login", json={"username": "admin", "password": passwords["admin"]})
    assert r.status_code == 200 and r.json()["username"] == "admin"
    assert r.json()["token"].startswith("lyk_")


def test_sso_login_with_a_colliding_username_gets_its_own_profile(client, auth_db, fake_oidc, bearer):
    callback, exchange = _sso_login(client)
    assert callback.status_code in (302, 307) and exchange is not None
    assert exchange.status_code == 200 and exchange.json()["username"] == "admin_oidc"
    # The new profile is a plain SSO user — not the local admin.
    assert client.get("/api/users", headers=bearer(exchange.json()["token"])).status_code == 403


def test_sso_login_is_denied_when_linking_fails(client, auth_db, fake_oidc, monkeypatch):
    """The audit's L2 scenario: the losing side of a first-login race used to
    receive a session for the raw external username ``admin`` (INT_ADMIN)."""

    def boom(*_a, **_kw):
        raise IntegrityError("INSERT", {}, Exception("duplicate"))

    monkeypatch.setattr(identity_link_service, "_resolve_once", boom)
    callback, exchange = _sso_login(client)

    assert exchange is None                                   # no exchange code was issued
    assert callback.headers["location"] == "/login?error=link_failed"
    with auth_db() as s:
        assert s.query(UserApiKey).count() == 0


def test_registry_never_falls_back_to_the_provider_result(auth_db, monkeypatch):
    import asyncio

    def boom(_result):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(identity_link_service, "resolve", boom)
    with pytest.raises(IdentityLinkError):
        asyncio.run(provider_registry.link_identity(_external()))
