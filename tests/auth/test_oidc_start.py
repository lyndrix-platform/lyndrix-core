"""AZ-08 (cheap parts) — the SPA's SSO start endpoint.

``next=//host`` passed as "a path", ``redirect_uri`` was only checked for its
origin (userinfo, query and fragment went through, and the one-time login code
is appended to that URL), and every call stored a login state without limit.
"""
import pytest

from core.api import auth_api
from core.components.auth.logic.providers.base import AuthProvider
from core.components.auth.logic.providers.registry import provider_registry

CALLBACK = "http://localhost:5173/auth/callback"       # origin is in the default CORS allowlist


class FakeOIDC(AuthProvider):
    provider_id = "oidc"
    display_name = "SSO"

    def __init__(self):
        self.started = []

    def is_configured(self):
        return True

    def is_sso(self):
        return True

    async def authenticate(self, username, password):
        return None

    async def build_login_url(self, flow="nicegui", redirect="/"):
        self.started.append((flow, redirect))
        return "https://idp.invalid/authorize?state=x"


@pytest.fixture()
def oidc(auth_db, monkeypatch):
    provider = FakeOIDC()
    monkeypatch.setattr(provider_registry, "_providers", {"oidc": provider})
    monkeypatch.setattr(auth_api, "_OIDC_START_PER_ADDRESS",
                        auth_api.SlidingWindowRateLimiter(max_attempts=30, window_seconds=60))
    monkeypatch.setattr(auth_api, "_OIDC_START_GLOBAL",
                        auth_api.SlidingWindowRateLimiter(max_attempts=600, window_seconds=60))
    return provider


def _start(client, **params):
    return client.get("/api/auth/oidc/start", params=params, follow_redirects=False)


def test_the_shells_own_request_still_works(client, oidc):
    r = _start(client, redirect_uri=CALLBACK, next="/dashboard")
    assert r.status_code == 302 and r.headers["location"].startswith("https://idp.invalid/")
    assert oidc.started == [("react", f"{CALLBACK}|/dashboard")]


@pytest.mark.parametrize("next_path", [
    "//evil.invalid/x", "/\\evil.invalid", "https://evil.invalid/", "evil", "/\t/evil.invalid",
    "/\n/evil.invalid", "",
])
def test_next_that_is_not_an_in_app_path_becomes_root(client, oidc, next_path):
    assert _start(client, redirect_uri=CALLBACK, next=next_path).status_code == 302
    assert oidc.started[-1] == ("react", f"{CALLBACK}|/")


@pytest.mark.parametrize("redirect_uri", [
    "https://attacker.invalid/cb",                        # foreign origin
    "http://localhost:5173@attacker.invalid/cb",          # userinfo: really attacker.invalid
    "http://user:pw@localhost:5173/auth/callback",
    "http://localhost:5173\\@attacker.invalid/cb",        # backslash confusion
    "http://localhost:5173/redir?to=https://attacker.invalid/",   # open redirect carries the code along
    "http://localhost:5173/auth/callback#frag",
    "http://localhost:5173/auth/call back",
    "//localhost:5173/auth/callback",                     # no scheme
    "/auth/callback",
    "",
])
def test_redirect_uri_must_be_a_plain_url_on_an_allowed_origin(client, oidc, redirect_uri):
    r = _start(client, redirect_uri=redirect_uri, next="/")
    assert r.status_code == 400 and r.json()["detail"].startswith("redirect_uri")
    assert oidc.started == []                             # no login state was created


def test_origin_comparison_ignores_case_only(client, oidc):
    assert _start(client, redirect_uri="HTTP://LOCALHOST:5173/auth/callback").status_code == 302
    assert _start(client, redirect_uri="http://localhost:5174/auth/callback").status_code == 400


def test_state_creation_is_rate_limited_per_client_address(client_at, oidc):
    one = client_at("203.0.113.5")
    codes = [_start(one, redirect_uri=CALLBACK).status_code for _ in range(32)]
    assert codes == [302] * 30 + [429, 429]
    blocked = _start(one, redirect_uri=CALLBACK)
    assert int(blocked.headers["retry-after"]) >= 1 and len(oidc.started) == 30
    assert _start(client_at("203.0.113.6"), redirect_uri=CALLBACK).status_code == 302


def test_rejected_requests_do_not_use_up_the_budget(client, oidc):
    for _ in range(100):
        assert _start(client, redirect_uri="https://attacker.invalid/cb").status_code == 400
    assert _start(client, redirect_uri=CALLBACK).status_code == 302


def test_limiter_forgets_idle_keys():
    from core.components.vault.logic.rate_limit import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60)
    limiter._MAX_KEYS = 50
    for i in range(500):
        limiter.record(f"addr-{i}")
    assert len(limiter._attempts) <= 51
