"""AZ-13 (limiter part) — Vault init/unseal throttling.

Keyed on the client address only, the limiter let everybody behind one
untrusted load-balancer address use up the operator's budget, while a caller
rotating addresses had no limit at all.
"""
import pytest

import core.services  # noqa: F401
from core.components.vault.logic import rate_limit
from core.components.vault.logic.rate_limit import SlidingWindowRateLimiter


@pytest.fixture(autouse=True)
def fresh_limiters(monkeypatch):
    monkeypatch.setattr(rate_limit, "unseal_limiter", SlidingWindowRateLimiter(5, 60))
    monkeypatch.setattr(rate_limit, "unseal_global_limiter", SlidingWindowRateLimiter(20, 60))


def test_one_address_gets_five_attempts_a_minute():
    assert [rate_limit.unseal_retry_after("203.0.113.1") for _ in range(5)] == [0] * 5
    assert 1 <= rate_limit.unseal_retry_after("203.0.113.1") <= 60


def test_one_address_cannot_use_up_everybodys_budget():
    """Everything behind one load-balancer address floods junk attempts: the
    operator still gets in from another address (a port-forward)."""
    for _ in range(500):
        rate_limit.unseal_retry_after("10.0.0.1")
    assert rate_limit.unseal_retry_after("127.0.0.1") == 0


def test_rotating_addresses_hits_the_global_cap():
    admitted = sum(rate_limit.unseal_retry_after(f"198.51.100.{i}") == 0 for i in range(100))
    assert admitted == 20
    assert rate_limit.unseal_retry_after("127.0.0.1") > 0      # bounded: at most one window


def test_refused_attempts_are_not_counted():
    for _ in range(5):
        rate_limit.unseal_retry_after("10.0.0.1")
    for _ in range(100):
        assert rate_limit.unseal_retry_after("10.0.0.1") > 0
    # Only the 5 admitted attempts used the global budget: 15 are left.
    assert sum(rate_limit.unseal_retry_after(f"198.51.100.{i}") == 0 for i in range(30)) == 15


def test_rest_endpoints_use_both_budgets():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.components.vault.api.vault_api import vault_api_router

    app = FastAPI()
    app.include_router(vault_api_router)

    def unseal(address):
        client = TestClient(app, client=(address, 50000), raise_server_exceptions=False)
        return client.post("/api/vault/unseal", json={"key": "dummy-master-key-not-real"}).status_code

    codes = [unseal("203.0.113.1") for _ in range(6)]
    assert 429 not in codes[:5] and codes[5] == 429            # admitted ones reach the (absent) Vault
    assert unseal("203.0.113.2") != 429
    # IPv6: one /64 is one source.
    codes = [unseal(f"2001:db8:1:2::{i}") for i in range(1, 7)]
    assert 429 not in codes[:5] and codes[5] == 429
