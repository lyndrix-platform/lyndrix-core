"""AZ-11 — unauthenticated robustness.

A single non-ASCII byte in ``X-API-Key`` made ``hmac.compare_digest(str, str)``
raise and every route — the public ones too — answer 500 whenever a system
key was configured; ``/api/events`` accepted any number of streams.
"""
import asyncio

import pytest
from fastapi import HTTPException

from config import settings
from core.api import events_api, security
from core.api.security import ApiIdentity, constant_time_equals

SYSTEM_KEY = "dummy-system-key-not-real"


@pytest.fixture()
def system_key(monkeypatch):
    monkeypatch.setenv("LYNDRIX_SYSTEM_API_KEY", SYSTEM_KEY)
    return {"X-API-Key": SYSTEM_KEY}


# ── key comparison ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("header", [
    {"X-API-Key": b"\xe9t\xe9"},
    {"X-API-Key": b"\xff\xfe"},
    {"Authorization": b"Bearer \xe9\xe8"},
    {"Authorization": b"Bearer lyk_\xe9"},
])
def test_non_ascii_credentials_are_a_plain_401_never_a_500(client, system_key, header):
    assert client.get("/api/auth/whoami", headers=header).json()["authenticated"] is False
    assert client.get("/api/health", headers=header).status_code == 200
    assert client.get("/api/auth/me", headers=header).status_code == 401


def test_the_system_key_still_authenticates(client, system_key):
    who = client.get("/api/auth/whoami", headers=system_key).json()
    assert who["authenticated"] is True and who["method"] == "api_key"
    assert client.get("/api/auth/me", headers={"X-API-Key": SYSTEM_KEY + "x"}).status_code == 401


def test_constant_time_equals_handles_any_text():
    assert constant_time_equals("secret", "secret") is True
    assert constant_time_equals("sécret", "sécret") is True
    assert constant_time_equals("\xe9", "e") is False
    assert constant_time_equals("secret", "secreT") is False
    assert constant_time_equals(None, "x") is False and constant_time_equals("x", None) is False


def test_webhook_token_with_non_ascii_bytes_is_rejected_not_crashing(monkeypatch):
    from core.components.notifications import api as notifications_api

    import asyncio

    class Ctx:
        # The check reads the token through the async accessor (off the event loop).
        async def aget_secret(self, key):
            return "dummy-webhook-token-not-real"

    monkeypatch.setattr(notifications_api.notification_service, "ctx", Ctx(), raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(notifications_api._require_webhook_token("t\xf6ken"))
    assert exc.value.status_code == 401
    # the right one passes
    asyncio.run(notifications_api._require_webhook_token("dummy-webhook-token-not-real"))


# ── event stream cap ────────────────────────────────────────────────────────


class _Request:
    """Minimal request for calling the stream handler without a network."""

    def __init__(self, host="203.0.113.5"):
        self.client = type("Client", (), {"host": host})()

    async def is_disconnected(self):
        return False


@pytest.fixture()
def streams(monkeypatch):
    """Isolated stream registry; returns a helper that opens one stream."""
    monkeypatch.setattr(events_api, "_SSE_QUEUES", set())
    monkeypatch.setattr(events_api, "_SSE_OWNERS", events_api.weakref.WeakKeyDictionary())
    monkeypatch.setattr(settings, "LYNDRIX_SSE_MAX_CONNECTIONS_PER_USER", 3)

    def open_stream(username=None, host="203.0.113.5"):
        identity = ApiIdentity(method="user_api_key", username=username, permissions=frozenset()) if username else None
        return asyncio.run(events_api.sse_stream(_Request(host), ticket=None, identity=identity))

    return open_stream


def test_a_user_cannot_open_streams_without_bound(streams):
    for _ in range(3):
        streams("alice")
    with pytest.raises(HTTPException) as exc:
        streams("alice")
    assert exc.value.status_code == 429 and exc.value.headers["Retry-After"]
    streams("bob")                                         # other users are not affected
    assert len(events_api._SSE_QUEUES) == 4                # the refused one registered nothing


def test_a_closed_stream_frees_its_slot(streams):
    for _ in range(3):
        streams("alice")
    events_api._SSE_QUEUES.pop()                           # what the generator's finally does
    streams("alice")
    with pytest.raises(HTTPException):
        streams("alice")


def test_streams_without_a_login_are_capped_per_client_address(streams):
    for _ in range(3 * events_api._ANONYMOUS_CAP_FACTOR):
        streams(host="198.51.100.7")
    with pytest.raises(HTTPException):
        streams(host="198.51.100.7")
    streams(host="198.51.100.8")
    streams("alice", host="198.51.100.7")                  # a login is counted per user, not per address


def test_the_cap_can_be_switched_off(streams, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_SSE_MAX_CONNECTIONS_PER_USER", 0)
    for _ in range(50):
        streams("alice")


def test_default_cap_is_generous():
    assert type(settings).model_fields["LYNDRIX_SSE_MAX_CONNECTIONS_PER_USER"].default == 20
