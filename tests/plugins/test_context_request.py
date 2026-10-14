"""Tests for ``ModuleContext.request`` — correlated request/response on the bus.

The bus itself is fire-and-forget; ``request`` adds a ``request_id`` to the
outgoing payload, waits for a reply on ``reply_topic`` carrying the same id,
and returns ``None`` on timeout so callers can degrade when the responder
plugin is absent.

Run inside the lyndrix-core runtime:
    pytest tests/plugins/test_context_request.py -v
"""
import asyncio

import pytest

from core.bus import bus
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

REQ = "test_req:identify"
REP = "test_req:identified"


def _ctx(plugin_id: str = "lyndrix.plugin.test_requester", *, emit=(REQ,), subscribe=(REP,)):
    manifest = ModuleManifest(
        id=plugin_id,
        name="Requester",
        version="0.0.1",
        type="PLUGIN",
        permissions=ModulePermissions(emit=list(emit), subscribe=list(subscribe)),
    )
    return ModuleContext(manifest)


def _responder(reply: dict | None = None, *, echo_id: bool = True):
    """Subscribe a sync responder on REQ; returns (callback, received-list)."""
    received: list[dict] = []

    def _on_request(payload):
        received.append(payload)
        out = dict(reply or {"ok": True})
        if echo_id:
            out["request_id"] = payload["request_id"]
        bus.emit(REP, out)

    bus.subscribe(REQ)(_on_request)
    return _on_request, received


def _run(coro):
    return asyncio.run(coro)


def test_request_returns_matching_reply():
    ctx = _ctx()
    cb, received = _responder({"uuid": "abc"})
    try:
        reply = _run(ctx.request(REQ, {"name": "host-1"}, reply_topic=REP, timeout=2))
        assert reply == {"uuid": "abc", "request_id": received[0]["request_id"]}
        assert received[0]["name"] == "host-1"
        assert ctx._pending_requests == {}  # cleaned up after completion
    finally:
        bus.unsubscribe(REQ, cb)
        ctx.dispose()


def test_request_times_out_to_none_when_nobody_answers():
    ctx = _ctx()
    try:
        reply = _run(ctx.request(REQ, {}, reply_topic=REP, timeout=0.05))
        assert reply is None
        assert ctx._pending_requests == {}  # timed-out future removed
    finally:
        ctx.dispose()


def test_reply_with_foreign_request_id_is_ignored():
    ctx = _ctx()
    cb, _ = _responder({"ok": True}, echo_id=False)  # replies without our id

    def _wrong_id(payload):
        bus.emit(REP, {"request_id": "not-ours", "ok": True})

    bus.subscribe(REQ)(_wrong_id)
    try:
        reply = _run(ctx.request(REQ, {}, reply_topic=REP, timeout=0.05))
        assert reply is None
    finally:
        bus.unsubscribe(REQ, cb)
        bus.unsubscribe(REQ, _wrong_id)
        ctx.dispose()


def test_two_plugins_on_same_reply_topic_each_get_their_own_reply():
    """The bus de-dups subscribers by qualname — listeners must stay distinct."""
    a, b = _ctx("lyndrix.plugin.a"), _ctx("lyndrix.plugin.b")
    cb, received = _responder()
    try:
        async def both():
            return await asyncio.gather(
                a.request(REQ, {"who": "a"}, reply_topic=REP, timeout=2),
                b.request(REQ, {"who": "b"}, reply_topic=REP, timeout=2),
            )

        ra, rb = _run(both())
        ids = {p["who"]: p["request_id"] for p in received}
        assert ra["request_id"] == ids["a"]
        assert rb["request_id"] == ids["b"]
    finally:
        bus.unsubscribe(REQ, cb)
        a.dispose()
        b.dispose()


def test_request_respects_manifest_permissions():
    no_emit = _ctx("lyndrix.plugin.no_emit", emit=(), subscribe=(REP,))
    no_sub = _ctx("lyndrix.plugin.no_sub", emit=(REQ,), subscribe=())
    try:
        with pytest.raises(PermissionError):
            _run(no_emit.request(REQ, {}, reply_topic=REP, timeout=0.05))
        with pytest.raises(PermissionError):
            _run(no_sub.request(REQ, {}, reply_topic=REP, timeout=0.05))
    finally:
        no_emit.dispose()
        no_sub.dispose()


def test_dispose_unsubscribes_reply_listener_and_frees_dedup_key():
    """After dispose a fresh context with the same id must be able to listen again."""
    first = _ctx("lyndrix.plugin.reuse")
    _run(first.request(REQ, {}, reply_topic=REP, timeout=0.01))
    first.dispose()
    assert REP not in first._reply_listeners

    second = _ctx("lyndrix.plugin.reuse")
    cb, _ = _responder({"second": True})
    try:
        reply = _run(second.request(REQ, {}, reply_topic=REP, timeout=2))
        assert reply is not None and reply["second"] is True
    finally:
        bus.unsubscribe(REQ, cb)
        second.dispose()
