"""SSE stream behaviour: overflow, audience snapshot, topic gates.

* BG-04 — a client whose queue overflows must get its stream ENDED (the React
  shell only reconnects when the response closes), not a zombie stream that
  receives keep-alives forever.
* BG-12 #1 — permissions are a per-connection snapshot refreshed off-loop; a
  revoked permission / API key or a deleted user takes effect at the next refresh.
* AZ-06 — permission-gated notifications and operator topics only reach
  connections whose user holds the permission.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.api import events_api
from core.components.auth.logic import access_service
from core.components.auth.logic.models import Group, User, UserApiKey
from core.components.database.logic.db_service import db_instance
from core.components.notifications.logic import notification_service as ns_mod

GATED = "feature:plugins.manage"


class FakeRequest:
    headers: dict = {}

    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for model in (User, Group, UserApiKey):
        model.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(db_instance, "SessionLocal", Session, raising=False)
    with Session() as s:
        s.add(User(username="operator", roles=[], groups=[], extra_permissions=[GATED]))
        s.add(User(username="viewer", roles=[], groups=[], extra_permissions=[]))
        s.add(User(username="root", roles=["superadmin"], groups=[], extra_permissions=[]))
        s.add(UserApiKey(id=7, username="operator", hashed_key="x" * 64, scopes=[], revoked=False))
        s.commit()
    access_service.invalidate_cache()
    yield Session
    access_service.invalidate_cache()


@pytest.fixture(autouse=True)
def clean_queues():
    saved = set(events_api._SSE_QUEUES)
    events_api._SSE_QUEUES.clear()
    yield
    events_api._SSE_QUEUES.clear()
    events_api._SSE_QUEUES.update(saved)


async def _open(cred, *, allowed=events_api._AUTH_TOPICS):
    """Open a stream the way ``sse_stream`` does; returns (queue, generator)."""
    audience = await events_api._refresh_audience(cred) if cred else events_api._UNKNOWN
    q: asyncio.Queue = asyncio.Queue(maxsize=events_api._QUEUE_SIZE)
    events_api._SSE_QUEUES.add(q)
    username = cred.username if cred else None
    return q, events_api._stream(FakeRequest(), q, allowed, username, cred, audience)


async def _frames(gen, count: int, timeout: float = 1.0) -> list[str]:
    out = []
    for _ in range(count):
        try:
            out.append(await asyncio.wait_for(gen.__anext__(), timeout))
        except (StopAsyncIteration, asyncio.TimeoutError):
            break
    return out


def _payloads(frames: list[str]) -> list[dict]:
    return [json.loads(f[len("data: "):]) for f in frames if f.startswith("data: ")]


def _cred(username: str, **kw) -> events_api._Credential:
    return events_api._Credential(username=username, **kw)


# --- BG-04 overflow ------------------------------------------------------------

def test_overflow_ends_the_stream_with_a_reconnect_event(db):
    async def run():
        q, gen = await _open(_cred("operator"))
        for i in range(events_api._QUEUE_SIZE + 5):          # client is not reading
            events_api._publish("nav:changed", {"i": i})
        assert q not in events_api._SSE_QUEUES               # out of the fan-out …
        first = await gen.__anext__()                        # … and the NEXT frame ends it
        assert first.startswith("event: reconnect") and '"overflow"' in first
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()                            # response body ends → EventSource reconnects

    asyncio.run(run())


def test_lossy_metrics_do_not_overflow_a_slow_client(db):
    async def run():
        q, _gen = await _open(_cred("operator"))
        for _ in range(events_api._QUEUE_SIZE * 2):
            events_api._publish("system:metrics_update", {"cpu": 1})
        assert q in events_api._SSE_QUEUES and q.qsize() <= events_api._QUEUE_SIZE // 2 + 1

    asyncio.run(run())


def test_close_all_streams_also_closes_a_full_queue(db):
    async def run():
        q, gen = await _open(_cred("operator"))
        while not q.full():
            q.put_nowait(json.dumps({"topic": "nav:changed", "payload": {}}))
        assert events_api.close_all_streams("reconnect") == 1
        assert (await gen.__anext__()).startswith("event: reconnect")

    asyncio.run(run())


# --- AZ-06 notification audience -------------------------------------------------

def _notification_payload(monkeypatch, required_permission):
    """What ``NotificationService.persist`` really puts on the bus."""
    emitted: list[tuple[str, dict]] = []
    svc = ns_mod.NotificationService()
    monkeypatch.setattr(ns_mod.bus, "emit", lambda topic, payload=None, **kw: emitted.append((topic, payload)))
    monkeypatch.setattr(svc, "_request_save", lambda: None)
    svc.persist(
        {"id": "n-1", "title": "Plugin install failed", "message": "repo secret-repo: boom",
         "type": "negative", "toast": False, "persist": True,
         "required_permission": required_permission,
         "source_plugin_id": "lyndrix.core.plugins", "endpoint_name": "install_failed"},
        broadcast=False, emit_outbound=False,
    )
    return next(payload for topic, payload in emitted if topic == "notification:new")


def test_persist_carries_the_audience_gate_on_the_bus(monkeypatch):
    payload = _notification_payload(monkeypatch, GATED)
    assert payload["required_permission"] == GATED
    assert payload["source_plugin_id"] == "lyndrix.core.plugins"
    assert payload["endpoint_name"] == "install_failed"


def test_gated_notification_reaches_only_permitted_users(db, monkeypatch):
    payload = _notification_payload(monkeypatch, GATED)

    async def run():
        _q1, operator = await _open(_cred("operator"))
        _q2, viewer = await _open(_cred("viewer"))
        _q3, root = await _open(_cred("root"))
        events_api._publish("notification:new", payload)
        events_api._publish("nav:changed", {})               # marker every stream gets

        got_operator = _payloads(await _frames(operator, 2))
        got_viewer = _payloads(await _frames(viewer, 2, timeout=0.2))
        got_root = _payloads(await _frames(root, 2))

        assert [m["topic"] for m in got_operator] == ["notification:new", "nav:changed"]
        assert [m["topic"] for m in got_root] == ["notification:new", "nav:changed"]
        assert [m["topic"] for m in got_viewer] == ["nav:changed"]      # gated body never sent
        sent = got_operator[0]["payload"]
        assert sent["message"] == "repo secret-repo: boom"
        assert not (set(sent) & events_api._NOTIFICATION_INTERNAL_KEYS)  # internals stripped

    asyncio.run(run())


def test_ungated_notification_still_reaches_everyone(db, monkeypatch):
    payload = _notification_payload(monkeypatch, None)

    async def run():
        _q, viewer = await _open(_cred("viewer"))
        events_api._publish("notification:new", payload)
        got = _payloads(await _frames(viewer, 1))
        assert got[0]["payload"]["title"] == "Plugin install failed"
        assert "required_permission" not in got[0]["payload"]

    asyncio.run(run())


# --- AZ-06 topic gates -----------------------------------------------------------

def test_operator_topics_are_gated_and_shell_topics_keep_flowing(db):
    async def run():
        _q1, operator = await _open(_cred("operator"))
        _q2, viewer = await _open(_cred("viewer"))
        events_api._publish("plugin:install_failed", {"repo": "org/secret-repo", "error": "token rejected"})
        events_api._publish("plugin:operation_changed", {"id": 3})
        events_api._publish("cluster:changed", {"instances": ["a", "b"]})
        events_api._publish("plugin:state_changed", {"plugin_id": "p.x", "action": "installed", "by": "admin"})
        events_api._publish("plugin:runtime_status", {"plugin_id": "p.x", "phase": "starting", "message": "warm-up"})
        events_api._publish("ui:needs_refresh", {"reason": "Plugin p.x installed."})
        events_api._publish("nav:changed", {})

        got_viewer = _payloads(await _frames(viewer, 4))
        assert [m["topic"] for m in got_viewer] == [
            "plugin:state_changed", "plugin:runtime_status", "ui:needs_refresh", "nav:changed",
        ]
        assert got_viewer[0]["payload"] == {"plugin_id": "p.x", "action": "installed"}   # reduced
        assert got_viewer[1]["payload"]["message"] == "warm-up"                           # warm-up panel intact
        assert got_viewer[2]["payload"] == {}

        got_operator = _payloads(await _frames(operator, 6))
        topics = [m["topic"] for m in got_operator]
        assert topics[:2] == ["plugin:install_failed", "plugin:operation_changed"]
        assert "cluster:changed" not in topics                # needs admin:read, operator has only plugins.manage
        assert got_operator[2]["payload"]["by"] == "admin"   # full payload for plugin admins

    asyncio.run(run())


def test_system_key_ticket_receives_everything(db):
    async def run():
        ticket = events_api._mint_stream_ticket("system", is_system=True)
        cred = events_api._credential_from_ticket(events_api._verify_stream_ticket_claims(ticket))
        _q, gen = await _open(cred)                           # "system" has no user row
        events_api._publish("cluster:changed", {"n": 2})
        assert _payloads(await _frames(gen, 1))[0]["topic"] == "cluster:changed"

    asyncio.run(run())


# --- BG-12 #1 snapshot refresh -----------------------------------------------------

def test_permission_check_does_no_db_work_per_message(db, monkeypatch):
    async def run():
        _q, gen = await _open(_cred("operator"))

        def boom(*_a, **_k):
            raise AssertionError("DB lookup on the event loop per message")

        monkeypatch.setattr(events_api, "_load_audience", boom)
        monkeypatch.setattr(access_service, "get_effective_permissions", boom)
        for i in range(20):
            events_api._publish("notification:new", {"id": i, "title": "t", "required_permission": GATED})
        assert len(_payloads(await _frames(gen, 20))) == 20

    asyncio.run(run())


def test_revoked_permission_takes_effect_at_the_next_refresh(db, monkeypatch):
    monkeypatch.setattr(events_api, "_AUDIENCE_REFRESH_SECONDS", 0.05)

    async def run():
        _q, gen = await _open(_cred("operator"))
        events_api._publish("notification:new", {"id": 1, "title": "before", "required_permission": GATED})
        assert _payloads(await _frames(gen, 1))[0]["payload"]["title"] == "before"

        with db() as s:
            s.query(User).filter(User.username == "operator").update({"extra_permissions": []})
            s.commit()
        access_service.invalidate_cache()
        await asyncio.sleep(0.08)

        events_api._publish("notification:new", {"id": 2, "title": "after", "required_permission": GATED})
        events_api._publish("nav:changed", {})
        assert [m["topic"] for m in _payloads(await _frames(gen, 2, timeout=0.3))] == ["nav:changed"]

    asyncio.run(run())


def test_deleted_user_stream_ends_at_the_next_refresh(db, monkeypatch):
    monkeypatch.setattr(events_api, "_AUDIENCE_REFRESH_SECONDS", 0.05)
    monkeypatch.setattr(events_api, "_KEEPALIVE_SECONDS", 0.06)

    async def run():
        _q, gen = await _open(_cred("viewer"))
        with db() as s:
            s.query(User).filter(User.username == "viewer").delete()
            s.commit()
        frames = await _frames(gen, 5, timeout=1.0)          # idle stream: ends on a keep-alive wake-up
        assert frames[-1].startswith("event: reconnect") and '"revoked"' in frames[-1]
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(run())


def test_revoked_api_key_behind_a_ticket_ends_the_stream(db, monkeypatch):
    monkeypatch.setattr(events_api, "_AUDIENCE_REFRESH_SECONDS", 0.05)

    async def run():
        ticket = events_api._mint_stream_ticket("operator", key_id=7)
        cred = events_api._credential_from_ticket(events_api._verify_stream_ticket_claims(ticket))
        assert cred.key_id == 7
        q, gen = await _open(cred)
        events_api._publish("nav:changed", {})                # start the stream (snapshot clock runs)
        assert len(await _frames(gen, 1)) == 1
        with db() as s:
            s.query(UserApiKey).filter(UserApiKey.id == 7).update({"revoked": True})
            s.commit()
        await asyncio.sleep(0.08)
        events_api._publish("nav:changed", {})
        frames = await _frames(gen, 2)
        assert frames[0].startswith("event: reconnect")

    asyncio.run(run())


def test_ticket_for_a_missing_user_is_not_an_authenticated_audience(db):
    audience = asyncio.run(events_api._refresh_audience(_cred("ghost")))
    assert audience.valid is False


def test_db_error_during_refresh_fails_closed_but_keeps_the_stream(db, monkeypatch):
    monkeypatch.setattr(events_api, "_AUDIENCE_REFRESH_SECONDS", 0.05)

    async def run():
        _q, gen = await _open(_cred("operator"))
        events_api._publish("nav:changed", {})                # start the stream (snapshot clock runs)
        assert len(await _frames(gen, 1)) == 1

        def down(_cred):
            raise RuntimeError("db down")

        monkeypatch.setattr(events_api, "_load_audience", down)
        await asyncio.sleep(0.08)
        events_api._publish("notification:new", {"id": 1, "title": "gated", "required_permission": GATED})
        events_api._publish("nav:changed", {})
        assert [m["topic"] for m in _payloads(await _frames(gen, 2, timeout=0.3))] == ["nav:changed"]

    asyncio.run(run())


def test_scoped_api_key_limits_the_audience(db):
    with db() as s:
        s.query(UserApiKey).filter(UserApiKey.id == 7).update({"scopes": ["api:read"]})
        s.commit()
    audience = asyncio.run(events_api._refresh_audience(_cred("operator", key_id=7)))
    assert audience.valid and not audience.allows(GATED)     # owner has it, the key's scopes do not


def test_old_tickets_without_extra_claims_still_verify():
    ticket = events_api._mint_stream_ticket("operator")
    assert events_api._verify_stream_ticket(ticket) == "operator"
    assert events_api._verify_stream_ticket(ticket + "x") is None
