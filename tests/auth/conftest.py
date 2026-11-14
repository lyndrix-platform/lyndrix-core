"""Shared fixtures for the authentication/authorization regression tests.

The tests drive the REAL FastAPI app in-process (``TestClient``, no startup
hooks, no network) on an in-memory SQLite database, so a route's whole
dependency chain — credential resolution, scopes, permission checks — is
exercised the way a request does it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before core.api)
from core.components.auth.logic import access_service
from core.components.auth.logic.hashing import hash_password
from core.components.auth.logic.models import (
    Group,
    RoleGrantLedger,
    User,
    UserApiKey,
    UserIdentity,
    UserPreference,
)
from core.components.database.logic.db_service import db_instance

ADMIN_PERMISSIONS = [
    "feature:dashboard.view", "feature:settings.edit", "feature:plugins.manage",
    "api:read", "api:write", "admin:read", "admin:write",
]
VIEWER_PERMISSIONS = ["feature:dashboard.view", "api:read"]

# Obviously fake test credentials (never real secrets).
PASSWORDS = {
    "admin": "dummy-admin-pass-not-real",
    "viewer": "dummy-viewer-pass-not-real",
    "breakglass": "dummy-breakglass-pass-not-real",
}

# Argon2 costs ~100 ms per hash; hash each dummy password once per test session.
_HASHES: dict[str, str] = {}


def _hash(password: str) -> str:
    if password not in _HASHES:
        _HASHES[password] = hash_password(password)
    return _HASHES[password]


@pytest.fixture(autouse=True)
def fresh_login_guard():
    """Every test starts with an empty throttle, verdict memory and shared counter."""
    from core.components.auth.logic import login_guard
    from core.components.cluster.logic.cluster_service import cluster_service

    def _reset():
        login_guard.reset_for_tests()
        # The login form's burst counter lives in the coordinator KV (in memory
        # for the single-instance "local" driver the tests run with).
        kv = getattr(cluster_service.coordinator, "_kv", None)
        if isinstance(kv, dict):
            kv.clear()

    _reset()
    yield
    _reset()


@pytest.fixture()
def auth_db(monkeypatch):
    """In-memory IAM database with the built-in groups and three accounts.

    * ``admin``      — INT_ADMIN member (admin:read/admin:write), no system flag
    * ``viewer``     — INT_VIEWER member (api:read only)
    * ``breakglass`` — ``superadmin`` system flag, no groups
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (User, Group, UserApiKey, UserIdentity, UserPreference, RoleGrantLedger):
        model.__table__.create(bind=engine)
    # Same session options as DatabaseService (autoflush off changes flush order).
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(db_instance, "SessionLocal", session_factory, raising=False)
    monkeypatch.setattr(db_instance, "is_connected", True, raising=False)

    with session_factory() as s:
        s.add(Group(name="INT_ADMIN", description="", permissions=ADMIN_PERMISSIONS,
                    ldap_mappings=[], roles=[]))
        s.add(Group(name="INT_VIEWER", description="", permissions=VIEWER_PERMISSIONS,
                    ldap_mappings=[], roles=[]))
        s.add(User(username="admin", hashed_password=_hash(PASSWORDS["admin"]), roles=[],
                   groups=["INT_ADMIN"], extra_permissions=[], email="admin@lyndrix.invalid"))
        s.add(User(username="viewer", hashed_password=_hash(PASSWORDS["viewer"]), roles=[],
                   groups=["INT_VIEWER"], extra_permissions=[]))
        s.add(User(username="breakglass", hashed_password=_hash(PASSWORDS["breakglass"]),
                   roles=["superadmin"], groups=[], extra_permissions=[]))
        s.commit()

    access_service.invalidate_cache()
    yield session_factory
    access_service.invalidate_cache()
    engine.dispose()


@pytest.fixture()
def passwords():
    """Plaintext dummy passwords of the seeded accounts."""
    return dict(PASSWORDS)


@pytest.fixture()
def fast_argon2(monkeypatch):
    """Replace the Argon2 comparison of the login path by a table lookup.

    Tests that fire dozens of wrong passwords would otherwise spend ~100 ms on
    each. Returns the list of (hash, password) comparisons that were made.
    """
    from core.components.auth.logic import login_guard

    calls: list[tuple[str, str]] = []

    def _verify(stored_hash: str, password: str) -> bool:
        calls.append((stored_hash, password))
        return _HASHES.get(password) == stored_hash

    monkeypatch.setattr(login_guard, "verify_password", _verify)
    return calls


@pytest.fixture()
def client_at(app):
    """A test client that connects from a given address."""
    from fastapi.testclient import TestClient

    def _client(address: str):
        return TestClient(app, raise_server_exceptions=False, client=(address, 50000))

    return _client


@pytest.fixture()
def app(auth_db, monkeypatch):
    """The real application object, past its boot phase."""
    import main
    from core.services import boot_service

    # While booting, the interceptor redirects every non-/api path to "/".
    monkeypatch.setattr(boot_service, "is_booting", False, raising=False)
    return main.app


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def mint_key(auth_db):
    """Create a ``lyk_`` key for a user and return the raw token."""
    from core.components.auth.logic.api_key_service import api_key_service

    def _mint(username: str, *, label: str = "test-key", scopes=None) -> str:
        raw, _info = api_key_service.create(username, label=label, scopes=scopes or [])
        return raw

    return _mint


@pytest.fixture()
def bearer():
    """Build the Authorization header for a raw token."""

    def _header(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    return _header
