"""AZ-02 — email provenance gates identity auto-linking.

Any user could set their own email to somebody else's address; that person's
first LDAP/OIDC login was then linked to the squatter's profile and their
directory groups were added to it. Auto-linking now needs an email that an
administrator or a provider put on the profile (``users.email_trusted``).
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.components.auth.logic import access_service, identity_link_service
from core.components.auth.logic.auth_service import auth_service
from core.components.auth.logic.models import User, UserIdentity, email_is_trusted
from core.components.auth.logic.providers.base import AuthResult
from core.components.database.logic.db_service import db_instance

BOSS_EMAIL = "boss@corp.invalid"


def _boss_login(**kw) -> AuthResult:
    """First directory login of 'boss' (mapped to INT_ADMIN) with a verified mail."""
    defaults = dict(username="boss", full_name="The Boss", email=BOSS_EMAIL, roles=["user"],
                    provider="ldap", provider_user_id="S-1-5-21-1-2-3-1105",
                    groups=["INT_ADMIN"], email_verified=True)
    defaults.update(kw)
    return AuthResult(**defaults)


def _user(auth_db, username) -> User:
    with auth_db() as s:
        return s.query(User).filter(User.username == username).one()


# ── the takeover ────────────────────────────────────────────────────────────


def test_self_service_email_is_not_a_link_target(client, auth_db, mint_key, bearer):
    token = mint_key("viewer")
    r = client.patch("/api/users/viewer", headers=bearer(token), json={"email": BOSS_EMAIL})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == BOSS_EMAIL and r.json()["user"]["email_trusted"] is False

    linked = identity_link_service.resolve(_boss_login())

    assert linked.username == "boss"                       # own, new profile
    assert _user(auth_db, "viewer").groups == ["INT_VIEWER"]
    access_service.invalidate_cache()
    assert client.get("/api/users", headers=bearer(token)).status_code == 403
    with auth_db() as s:
        viewer_id = s.query(User).filter(User.username == "viewer").one().id
        assert s.query(UserIdentity).filter(UserIdentity.user_id == viewer_id).count() == 0


# ── the legitimate flows keep working ───────────────────────────────────────


def test_admin_set_email_still_links(client, auth_db, mint_key, bearer):
    r = client.patch("/api/users/viewer", headers=bearer(mint_key("admin")), json={"email": BOSS_EMAIL})
    assert r.status_code == 200 and r.json()["user"]["email_trusted"] is True

    linked = identity_link_service.resolve(_boss_login(groups=[]))
    assert linked.username == "viewer"


def test_admin_created_user_links_by_email(client, auth_db, mint_key, bearer):
    r = client.post("/api/users", headers=bearer(mint_key("admin")), json={
        "username": "boss.local", "password": "dummy-pass-not-real", "email": BOSS_EMAIL})
    assert r.status_code == 201 and r.json()["user"]["email_trusted"] is True
    assert identity_link_service.resolve(_boss_login(groups=[])).username == "boss.local"


def test_rows_from_before_the_column_are_grandfathered(auth_db):
    with auth_db() as s:
        s.add(User(username="legacy", hashed_password=None, roles=[], groups=[], extra_permissions=[],
                   email=BOSS_EMAIL))
        s.commit()
        # NULL as an older core leaves it (the ORM default would write False).
        s.execute(text("UPDATE users SET email_trusted = NULL WHERE username = 'legacy'"))
        s.commit()
    assert identity_link_service.resolve(_boss_login(groups=[])).username == "legacy"


def test_echoing_the_stored_email_keeps_its_provenance(client, auth_db, mint_key, bearer):
    client.patch("/api/users/viewer", headers=bearer(mint_key("admin")), json={"email": BOSS_EMAIL})
    # The profile form always sends full_name AND email, changed or not.
    r = client.patch("/api/users/viewer", headers=bearer(mint_key("viewer")),
                     json={"full_name": "Vera Viewer", "email": BOSS_EMAIL.upper()})
    assert r.status_code == 200 and r.json()["user"]["email_trusted"] is True


def test_admin_changing_their_own_email_counts_as_an_admin_edit(client, auth_db, mint_key, bearer):
    r = client.patch("/api/users/admin", headers=bearer(mint_key("admin")), json={"email": BOSS_EMAIL})
    assert r.status_code == 200 and r.json()["user"]["email_trusted"] is True


def test_clearing_the_email_leaves_nothing_to_trust(client, auth_db, mint_key, bearer):
    r = client.patch("/api/users/admin", headers=bearer(mint_key("admin")), json={"email": ""})
    assert r.json()["user"]["email"] is None and r.json()["user"]["email_trusted"] is False


def test_provider_vouching_for_the_owners_address_restores_trust(client, auth_db, mint_key, bearer):
    with auth_db() as s:
        viewer = s.query(User).filter(User.username == "viewer").one()
        s.add(UserIdentity(user_id=viewer.id, provider="oidc", provider_user_id="sub-viewer",
                           matched_by="manual"))
        s.commit()
    client.patch("/api/users/viewer", headers=bearer(mint_key("viewer")), json={"email": "vera@corp.invalid"})
    assert email_is_trusted(_user(auth_db, "viewer")) is False

    own_login = dict(username="viewer", full_name="Vera", roles=["user"], provider="oidc",
                     provider_user_id="sub-viewer")
    # An unverified claim, or a verified claim for ANOTHER address, changes nothing.
    identity_link_service.resolve(AuthResult(email="vera@corp.invalid", email_verified=False, **own_login))
    identity_link_service.resolve(AuthResult(email="other@corp.invalid", email_verified=True, **own_login))
    assert email_is_trusted(_user(auth_db, "viewer")) is False

    identity_link_service.resolve(AuthResult(email="Vera@corp.invalid", email_verified=True, **own_login))
    assert email_is_trusted(_user(auth_db, "viewer")) is True


@pytest.mark.parametrize("verified", [True, False])
def test_new_sso_profile_trusts_its_email_only_when_the_provider_vouches(auth_db, verified):
    linked = identity_link_service.resolve(_boss_login(email_verified=verified, groups=[]))
    assert email_is_trusted(_user(auth_db, linked.username)) is verified


def test_me_reports_the_provenance(client, auth_db, mint_key, bearer):
    token = mint_key("viewer")
    client.patch("/api/users/viewer", headers=bearer(token), json={"email": "vera@corp.invalid"})
    me = client.get("/api/auth/me", headers=bearer(token)).json()
    assert me["email"] == "vera@corp.invalid" and me["email_trusted"] is False


def test_profile_without_email_is_never_trusted():
    assert email_is_trusted(User(username="x", email=None, email_trusted=None)) is False
    assert email_is_trusted(User(username="x", email="  ", email_trusted=True)) is False


# ── migration ───────────────────────────────────────────────────────────────


@pytest.fixture()
def legacy_engine(monkeypatch):
    """A database whose tables predate every additive column."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, username VARCHAR(50) NOT NULL UNIQUE, "
            "hashed_password VARCHAR(255), full_name VARCHAR(100), email VARCHAR(100), roles JSON)"
        ))
        conn.execute(text(
            'CREATE TABLE "groups" (id INTEGER PRIMARY KEY, name VARCHAR(100) UNIQUE, '
            "description VARCHAR(255), permissions JSON)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, username, email, roles) VALUES ('u1', 'old', 'old@corp.invalid', '[]')"
        ))
    monkeypatch.setattr(db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    yield engine
    engine.dispose()


def _columns(engine, table):
    with engine.connect() as conn:
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info('{table}')"))}


def test_migration_adds_the_column_and_keeps_existing_rows_trusted(legacy_engine):
    auth_service._migrate_schema()
    assert "email_trusted" in _columns(legacy_engine, "users")

    with db_instance.SessionLocal() as s:
        old = s.query(User).filter(User.username == "old").one()
        assert old.email_trusted is None and email_is_trusted(old) is True


def test_migration_is_idempotent_and_issues_no_ddl_once_applied(legacy_engine, monkeypatch):
    auth_service._migrate_schema()
    statements = []

    from sqlalchemy import event

    @event.listens_for(legacy_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, *_a):
        statements.append(statement)

    auth_service._migrate_schema()
    assert not [s for s in statements if s.upper().startswith("ALTER")]


def test_migration_tolerates_a_peer_adding_the_column_first(legacy_engine, monkeypatch, caplog):
    auth_service._migrate_schema()
    # Lost race: our existence check said "missing", the peer's ALTER landed first.
    monkeypatch.setattr(type(auth_service), "_column_exists", staticmethod(lambda *_a: False))
    with caplog.at_level("WARNING"):
        auth_service._migrate_schema()
    assert "Could not add" not in caplog.text
    assert "email_trusted" in _columns(legacy_engine, "users")


def test_old_core_inserts_without_the_column_still_work(legacy_engine):
    """A core that predates the column keeps writing rows during a rolling update."""
    auth_service._migrate_schema()
    with legacy_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, username, email, roles) VALUES ('u2', 'written-by-old-core', 'x@corp.invalid', '[]')"
        ))
    with db_instance.SessionLocal() as s:
        row = s.query(User).filter(User.username == "written-by-old-core").one()
        assert row.email_trusted is None and email_is_trusted(row) is True


class _LockedConn:
    """MariaDB stand-in: the first ``locked`` ALTERs hit lock_wait_timeout (1205)."""

    def __init__(self, locked):
        self.locked, self.statements, self.commits = locked, [], 0

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("ALTER") and self.locked:
            self.locked -= 1
            raise RuntimeError("(1205, 'Lock wait timeout exceeded; try restarting transaction')")

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_the_alter_waits_a_bounded_time_for_the_table_lock_and_retries(monkeypatch, caplog):
    """N4 (opus review): on MariaDB a queued ALTER waits up to a day for the
    metadata lock and blocks every login meanwhile."""
    import core.components.auth.logic.auth_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    conn = _LockedConn(locked=2)
    with caplog.at_level("WARNING"):
        auth_service._add_column(conn, "users", "email_trusted", "BOOLEAN NULL", mysql=True)
    alters = [s for s in conn.statements if s.startswith("ALTER")]
    assert len(alters) == 3 and conn.commits == 1
    assert conn.statements[0] == f"SET SESSION lock_wait_timeout = {auth_service._DDL_LOCK_WAIT_S}"
    assert conn.statements.count("SET SESSION lock_wait_timeout = DEFAULT") == 3
    assert "Could not add" not in caplog.text

    stuck = _LockedConn(locked=99)
    with caplog.at_level("WARNING"):
        auth_service._add_column(stuck, "users", "email_trusted", "BOOLEAN NULL", mysql=True)
    assert len([s for s in stuck.statements if s.startswith("ALTER")]) == auth_service._DDL_ATTEMPTS
    assert "Could not add `users.email_trusted`" in caplog.text
