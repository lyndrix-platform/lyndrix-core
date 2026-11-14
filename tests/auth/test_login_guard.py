"""AZ-01 — one throttled choke point for every password check.

Before the fix only ``POST /api/auth/login`` was limited (and that limiter
could be dodged with ``ádmin``-style spellings); HTTP Basic verified a password
on EVERY route — including the public ``/api/auth/whoami`` — without any
throttle, each attempt a 64 MiB Argon2 run.
"""
import asyncio
import base64
import threading
import time

import pytest

from config import settings
from core.components.auth.logic import login_guard
from core.components.auth.logic.auth_service import auth_service
from core.components.auth.logic.login_guard import LoginBusy, LoginThrottled
from core.components.auth.logic.login_throttle import (
    ACCOUNT_FRESH_RESERVE,
    ACCOUNT_REPEAT_LIMIT,
    login_throttle,
)
from core.components.auth.logic.providers.registry import provider_registry


def _basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class _Clock:
    """Moves the throttle's clock AND the shared burst window together."""

    def __init__(self, monkeypatch):
        self.offset = 0.0
        real_monotonic, real_wall = time.monotonic, time.time
        monkeypatch.setattr(login_throttle, "_clock", lambda: real_monotonic() + self.offset)
        monkeypatch.setattr(login_guard, "_wall_clock", lambda: real_wall() + self.offset)

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture()
def clock(monkeypatch):
    return _Clock(monkeypatch)


# ── bounded verification ────────────────────────────────────────────────────


def test_account_without_local_password_never_reaches_argon2(fast_argon2):
    assert login_guard.verify_login_password(None, "anything") is False
    assert login_guard.verify_login_password("", "anything") is False
    assert fast_argon2 == []


def test_a_confirmed_password_is_remembered_and_bound_to_the_stored_hash(fast_argon2, monkeypatch):
    monkeypatch.setattr(login_guard, "verify_password", lambda h, p: (fast_argon2.append((h, p)), p == "right")[1])
    assert login_guard.verify_login_password("hash-1", "right") is True
    assert login_guard.verify_login_password("hash-1", "right") is True
    assert len(fast_argon2) == 1                       # HTTP Basic: one Argon2 run, not one per request
    # A changed password is a different stored hash: nothing is remembered for it.
    assert login_guard.verify_login_password("hash-2", "right") is True
    assert len(fast_argon2) == 2


def test_a_rejected_password_is_remembered_too(monkeypatch):
    runs = []
    monkeypatch.setattr(login_guard, "verify_password", lambda h, p: (runs.append(p), False)[1])
    for _ in range(50):
        assert login_guard.verify_login_password("hash-1", "outdated") is False
    assert runs == ["outdated"]
    assert login_guard.verify_login_password("hash-1", "another guess") is False
    assert runs == ["outdated", "another guess"]


def test_argon2_runs_are_bounded_and_the_overflow_is_told_to_come_back(monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_LOGIN_VERIFY_CONCURRENCY", 2)
    monkeypatch.setattr(login_guard, "_SLOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(login_guard, "_WAITERS_PER_SLOT", 1)
    login_guard.reset_for_tests()

    release = threading.Event()
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def slow_verify(_hash, _password):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        release.wait(5)
        with lock:
            state["running"] -= 1
        return False

    monkeypatch.setattr(login_guard, "verify_password", slow_verify)
    outcomes: list = []

    def attempt(i):
        try:
            outcomes.append(login_guard.verify_login_password("hash", f"guess-{i}"))
        except LoginBusy as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    time.sleep(0.4)                                    # every waiter has given up by now
    release.set()
    for t in threads:
        t.join(5)

    busy = [o for o in outcomes if isinstance(o, LoginBusy)]
    assert state["peak"] == 2                          # never more than the configured slots
    assert len(outcomes) == 12 and len(busy) == 10
    assert all(o.status_code == 503 and o.retry_after >= 1 for o in busy)


def test_a_burst_of_one_clients_requests_costs_one_argon2_run(monkeypatch):
    """30 parallel requests with the same credentials right after a restart."""
    monkeypatch.setattr(settings, "LYNDRIX_LOGIN_VERIFY_CONCURRENCY", 1)
    login_guard.reset_for_tests()
    runs = []

    def verify(_hash, password):
        runs.append(password)
        time.sleep(0.05)
        return True

    monkeypatch.setattr(login_guard, "verify_password", verify)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(login_guard.verify_login_password("hash", "right")))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [True] * 6 and len(runs) == 1


# ── throttle bookkeeping of the guard ───────────────────────────────────────


def test_guard_blocks_after_five_distinct_failures_and_counts_nothing_without_a_verdict():
    def busy():
        raise LoginBusy("busy", 2)

    for _ in range(20):                                # "busy" is not a verdict: never counted
        with pytest.raises(LoginBusy):
            login_guard.guarded_verify("admin", "x", "10.0.0.1", busy)
    for i in range(5):
        assert login_guard.guarded_verify("admin", f"guess-{i}", "10.0.0.1", lambda: None) is None
    with pytest.raises(LoginThrottled) as exc:
        login_guard.guarded_verify("admin", "guess-6", "10.0.0.1", lambda: "never called")
    assert exc.value.status_code == 429 and 1 <= exc.value.retry_after <= 60


def test_guard_counts_a_repeated_wrong_password_once_unless_a_directory_was_asked():
    for _ in range(40):                                # local accounts: outdated password in a script
        assert login_guard.guarded_verify("bot", "outdated", "10.0.0.1", lambda: None) is None
    # With an external provider in the chain every failure was a bind there.
    for _ in range(5):
        login_guard.guarded_verify("ldapuser", "outdated", "10.0.0.1", lambda: None, external=True)
    with pytest.raises(LoginThrottled):
        login_guard.guarded_verify("ldapuser", "outdated", "10.0.0.1", lambda: None, external=True)


# ── HTTP Basic ──────────────────────────────────────────────────────────────


def test_basic_is_throttled_like_the_login_form(client, passwords, fast_argon2):
    codes = [client.get("/api/auth/me", headers=_basic("admin", f"wrong-{i}")).status_code for i in range(7)]
    assert codes == [401] * 5 + [429, 429]
    blocked = client.get("/api/auth/me", headers=_basic("admin", "wrong-again"))
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert len(fast_argon2) == 5                       # nothing is verified once blocked


def test_basic_is_not_advertised_and_correct_credentials_still_work(client, passwords, fast_argon2):
    r = client.get("/api/auth/me")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    ok = client.get("/api/auth/me", headers=_basic("admin", passwords["admin"]))
    assert ok.status_code == 200 and ok.json()["method"] == "basic"


def test_an_outdated_password_in_a_script_does_not_block_the_account(client, passwords, fast_argon2):
    codes = {client.get("/api/auth/me", headers=_basic("admin", "outdated")).status_code for _ in range(40)}
    assert codes == {401}
    assert len(fast_argon2) == 1                       # …and costs a single Argon2 run
    assert client.get("/api/auth/me", headers=_basic("admin", passwords["admin"])).status_code == 200


def test_public_routes_never_evaluate_basic(client, passwords, fast_argon2):
    for password in (passwords["admin"], "wrong"):
        who = client.get("/api/auth/whoami", headers=_basic("admin", password))
        assert who.status_code == 200 and who.json()["authenticated"] is False
        health = client.get("/api/health", headers=_basic("admin", password))
        assert health.status_code == 200 and "plugins" not in health.json()
    assert fast_argon2 == []                           # no password was checked at all


def test_basic_can_be_switched_off(client, passwords, fast_argon2, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_API_BASIC_AUTH", False)
    r = client.get("/api/auth/me", headers=_basic("admin", passwords["admin"]))
    assert r.status_code == 401 and fast_argon2 == []


def test_busy_verifier_answers_503_not_401(client, passwords, monkeypatch):
    def busy(_hash, _password):
        raise LoginBusy("The server is busy verifying logins. Try again shortly.", 2)

    monkeypatch.setattr(login_guard._verifier, "verify", busy)
    basic = client.get("/api/auth/me", headers=_basic("admin", passwords["admin"]))
    form = client.post("/api/auth/login", json={"username": "admin", "password": passwords["admin"]})
    assert basic.status_code == form.status_code == 503
    assert basic.headers["retry-after"] == form.headers["retry-after"] == "2"


def test_request_without_a_client_address_is_handled(auth_db, mint_key, passwords, fast_argon2):
    """Plugins authenticate synthetic requests (no ``client`` in the scope)."""
    from starlette.requests import Request

    from core.api import authenticate_request

    def synthetic(header: str) -> Request:
        return Request({"type": "http", "headers": [(b"authorization", header.encode())]})

    assert authenticate_request(synthetic(f"Bearer {mint_key('viewer')}")).username == "viewer"
    basic = _basic("admin", passwords["admin"])["Authorization"]
    assert authenticate_request(synthetic(basic)).username == "admin"


# ── login form ──────────────────────────────────────────────────────────────


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_login_form_contract_is_unchanged(client, passwords, fast_argon2, clock):
    codes = [_login(client, "admin", f"wrong-{i}").status_code for i in range(6)]
    assert codes == [401] * 5 + [429]
    blocked = _login(client, "admin", passwords["admin"])
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    clock.advance(61)
    assert _login(client, "admin", passwords["admin"]).status_code == 200


def test_accented_spellings_share_the_accounts_bucket(client, fast_argon2):
    spellings = ["admin", "ádmin", "àdmin", "ADMIN", "âdmin", "ädmin"]
    codes = [_login(client, name, f"wrong-{i}").status_code for i, name in enumerate(spellings)]
    assert codes == [401] * 5 + [429]


def test_basic_and_the_form_share_one_budget(client, passwords, fast_argon2):
    for i in range(3):
        assert client.get("/api/auth/me", headers=_basic("admin", f"wrong-{i}")).status_code == 401
    for i in range(3, 5):
        assert _login(client, "admin", f"wrong-{i}").status_code == 401
    assert _login(client, "admin", "wrong-5").status_code == 429
    assert client.get("/api/auth/me", headers=_basic("admin", "wrong-6")).status_code == 429


def test_rotating_addresses_cannot_guess_one_account_without_limit(client_at, passwords, fast_argon2):
    home = client_at("10.0.0.5")
    assert _login(home, "admin", passwords["admin"]).status_code == 200      # known-good from here

    codes = [
        _login(client_at(f"198.51.100.{i}"), "admin", f"guess-{i}").status_code
        for i in range(ACCOUNT_REPEAT_LIMIT + ACCOUNT_FRESH_RESERVE + 5)
    ]
    ceiling = ACCOUNT_REPEAT_LIMIT + ACCOUNT_FRESH_RESERVE
    assert codes == [401] * ceiling + [429] * 5

    # Even the CORRECT password is refused from an address the account never used…
    assert _login(client_at("203.0.113.77"), "admin", passwords["admin"]).status_code == 429
    # …but not from the address the user logged in from before, and not for other accounts.
    assert _login(home, "admin", passwords["admin"]).status_code == 200
    assert _login(client_at("203.0.113.78"), "viewer", passwords["viewer"]).status_code == 200


def test_plugin_facing_authenticate_user_never_raises(auth_db, passwords, fast_argon2):
    assert auth_service.authenticate_user("admin", passwords["admin"]).username == "admin"
    for i in range(5):
        assert auth_service.authenticate_user("admin", f"wrong-{i}", "10.0.0.9") is None
    with pytest.raises(LoginThrottled):
        auth_service.verify_local_login("admin", "wrong-x", "10.0.0.9")
    assert auth_service.authenticate_user("admin", passwords["admin"], "10.0.0.9") is None


def test_the_provider_chain_is_one_attempt_keyed_on_the_callers_address(auth_db, passwords, fast_argon2):
    """The NiceGUI form calls the registry directly — same choke point."""
    async def attempts():
        for i in range(5):
            assert await provider_registry.authenticate("admin", f"wrong-{i}", client_ip="10.0.0.7") is None
        with pytest.raises(LoginThrottled):
            await provider_registry.authenticate("admin", passwords["admin"], client_ip="10.0.0.7")
        other = await provider_registry.authenticate("admin", passwords["admin"], client_ip="10.0.0.8")
        assert other is not None and other.username == "admin"

    asyncio.run(attempts())


# ── a directory outage is not a wrong password ──────────────────────────────


class _DownDirectory:
    """A form provider whose backend cannot be reached."""

    provider_id = "ldap"
    display_name = "LDAP"

    def is_configured(self):
        return True

    def is_sso(self):
        return False

    async def authenticate(self, username, password):
        from core.components.auth.logic.providers.base import AuthProviderUnavailable

        raise AuthProviderUnavailable("directory unreachable")


@pytest.fixture()
def directory_down(auth_db, monkeypatch):
    from core.components.auth.logic.providers.base import AuthProvider

    provider = type("DownDirectory", (_DownDirectory, AuthProvider), {})()
    monkeypatch.setattr(provider_registry, "_providers", {"ldap": provider})
    return provider


def test_directory_outage_does_not_throttle_its_users(directory_down, fast_argon2, passwords):
    async def scenario():
        # A directory user keeps retrying while the directory is down…
        for _ in range(25):
            assert await provider_registry.authenticate("jdoe", "their-directory-password",
                                                        client_ip="10.0.0.7") is None
        # …and is not blocked: nothing was counted against the account or the pair.
        assert login_throttle.retry_after("jdoe", "10.0.0.7") == 0

        # A LOCAL account's wrong password WAS judged (local fallback) and counts,
        # outage or not — an unreachable directory must not open unlimited guessing.
        for i in range(5):
            assert await provider_registry.authenticate("admin", f"wrong-{i}", client_ip="10.0.0.8") is None
        with pytest.raises(LoginThrottled):
            await provider_registry.authenticate("admin", "wrong-5", client_ip="10.0.0.8")
        # The local admin still gets in from elsewhere while the directory is down.
        ok = await provider_registry.authenticate("admin", passwords["admin"], client_ip="10.0.0.9")
        assert ok is not None and ok.username == "admin"

    asyncio.run(scenario())


def test_ldap_provider_reports_an_unreachable_directory_as_unavailable(monkeypatch):
    import ldap3

    from core.components.auth.logic.providers.base import AuthProviderUnavailable
    from core.components.auth.logic.providers.ldap import LDAPProvider

    def unreachable(*_a, **_kw):
        raise ldap3.core.exceptions.LDAPSocketOpenError("connection refused")

    monkeypatch.setattr(ldap3, "Connection", unreachable)
    provider = LDAPProvider(url="ldap://directory.invalid", bind_dn="cn=svc", bind_password="dummy-not-real",
                            base_dn="dc=example")
    with pytest.raises(AuthProviderUnavailable):
        provider._authenticate_blocking("jdoe", "some-password")
    # An empty password is a wrong password, not an outage (and never an anonymous bind).
    assert provider._authenticate_blocking("jdoe", "") is None


# ── current-password confirmation ───────────────────────────────────────────


def test_current_password_check_is_no_unthrottled_oracle(client, mint_key, bearer, passwords, fast_argon2):
    headers = bearer(mint_key("viewer"))
    codes = [
        client.post("/api/users/viewer/password", headers=headers,
                    json={"current_password": f"wrong-{i}", "new_password": "dummy-new-pass-not-real"}).status_code
        for i in range(6)
    ]
    assert codes == [400] * 5 + [429]                  # never 401: that would log the browser out


def test_password_change_with_the_right_current_password_works(client, mint_key, bearer, passwords):
    r = client.post("/api/users/viewer/password", headers=bearer(mint_key("viewer")),
                    json={"current_password": passwords["viewer"], "new_password": "dummy-new-pass-not-real"})
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=_basic("viewer", "dummy-new-pass-not-real")).status_code == 200
    # The old password was remembered as correct — for the OLD hash only.
    assert client.get("/api/auth/me", headers=_basic("viewer", passwords["viewer"])).status_code == 401


# ── preflight ───────────────────────────────────────────────────────────────


def test_preflight_warns_when_every_sender_is_a_trusted_proxy(monkeypatch):
    from core.components.cluster.logic.cluster_service import cluster_service
    from core.components.cluster.logic.preflight import run_checks

    monkeypatch.setattr(settings, "LYNDRIX_TRUSTED_PROXIES", "*")
    checks = {c.id: c for c in asyncio.run(run_checks(cluster_service))}
    assert checks["trusted_proxies"].status == "warn"
    assert "X-Forwarded-For" in checks["trusted_proxies"].hint
    assert not [c for c in checks.values() if c.status == "fail"]      # a warning, not a failure

    monkeypatch.setattr(settings, "LYNDRIX_TRUSTED_PROXIES", "10.42.0.0/16")
    checks = {c.id: c for c in asyncio.run(run_checks(cluster_service))}
    assert checks["trusted_proxies"].status == "ok"
