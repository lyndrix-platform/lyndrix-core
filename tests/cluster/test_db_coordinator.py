"""``db`` coordinator driver against an in-memory SQLite engine shared by two
"instances" (same tables, different instance ids).

Run inside the lyndrix-core runtime:
    pytest tests/cluster -v
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.pool import StaticPool

from core.components.cluster.logic.drivers.db import DbCoordinator
from core.components.cluster.logic.models import ClusterEvent, ClusterInstance


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    yield eng
    eng.dispose()


def _coord(engine, iid: str, ttl: int = 30) -> DbCoordinator:
    c = DbCoordinator(instance_id=iid, ttl=ttl, heartbeat_s=1, poll_ms=100, engine_getter=lambda: engine)
    c.ensure_schema()
    return c


def test_lease_is_exclusive_renewable_and_releasable(engine):
    a, b = _coord(engine, "a"), _coord(engine, "b")

    async def run():
        assert await a.try_acquire("core:leader") is True
        assert await b.try_acquire("core:leader") is False
        assert await a.try_acquire("core:leader") is True  # renew keeps it
        info = await b.holder("core:leader")
        assert info.holder == "a" and info.expired is False and 0 < info.remaining_s <= 30
        assert info.acquired_at is not None
        assert await b.release("core:leader") is False  # not b's lease
        assert await a.release("core:leader") is True
        assert await b.try_acquire("core:leader") is True
        names = [l.name for l in await a.leases()]
        assert names == ["core:leader"]

    asyncio.run(run())


def test_expired_lease_is_taken_over_and_force_release_works(engine):
    a, b = _coord(engine, "a"), _coord(engine, "b")

    async def run():
        assert await a.try_acquire("x", ttl=1) is True
        assert await b.try_acquire("x", ttl=1) is False
        await asyncio.sleep(1.2)
        assert await b.try_acquire("x", ttl=1) is True  # a did not renew → b owns it
        assert (await a.holder("x")).holder == "b"
        assert await a.force_release("x") is True  # admin override, any holder
        assert await a.holder("x") is None

    asyncio.run(run())


def test_heartbeat_registers_instances_and_stop_deregisters(engine):
    a, b = _coord(engine, "a"), _coord(engine, "b")

    async def run():
        await a.start()
        await b.start()
        await a.heartbeat({"ready": True, "when": datetime(2026, 1, 1)})
        await b.heartbeat({"ready": False})
        seen = {i.instance_id: i for i in await a.instances()}
        assert set(seen) == {"a", "b"}
        assert seen["a"].is_self is True and seen["b"].is_self is False
        assert seen["a"].alive is True and seen["a"].age_s < 5
        assert seen["a"].meta == {"ready": True, "when": "2026-01-01 00:00:00"}  # JSON-safe
        assert seen["a"].core_version and seen["a"].api_version
        await a.heartbeat({"ready": True})  # second heartbeat = update, not duplicate
        assert len(await b.instances()) == 2
        await a.stop()
        await b.stop()
        assert await _coord(engine, "c").instances() == []

    asyncio.run(run())


def test_kv_set_get_ttl_incr_delete(engine):
    a = _coord(engine, "a")

    async def run():
        await a.kv_set("k", {"x": 1})
        assert await a.kv_get("k") == {"x": 1}
        await a.kv_set("k", {"x": 2})  # overwrite
        assert await a.kv_get("k") == {"x": 2}
        await a.kv_set("t", "v", ttl=1)
        assert await a.kv_get("t") == "v"
        await asyncio.sleep(1.2)
        assert await a.kv_get("t") is None  # expired
        assert await a.kv_incr("c", ttl=60) == 1
        assert await a.kv_incr("c", ttl=60) == 2
        assert await a.kv_delete("c") is True
        assert await a.kv_delete("c") is False
        assert await a.kv_incr("c") == 1
        assert await a.kv_get("missing") is None

    asyncio.run(run())


def test_events_reach_peers_but_never_the_publisher(engine):
    a, b = _coord(engine, "a"), _coord(engine, "b")
    got_b, got_a = [], []
    b.subscribe(lambda topic, payload, origin: got_b.append((topic, payload, origin)))
    a.subscribe(lambda topic, payload, origin: got_a.append(topic))

    async def run():
        await a.start()
        await b.start()
        await a.publish("plugin:state_changed", {"plugin_id": "x", "when": datetime(2026, 1, 1)})
        for _ in range(40):
            if got_b:
                break
            await asyncio.sleep(0.05)
        await a.stop()
        await b.stop()

    asyncio.run(run())
    assert got_b == [("plugin:state_changed", {"plugin_id": "x", "when": "2026-01-01 00:00:00"}, "a")]
    assert got_a == []
    assert a.events_published == 1 and b.events_received == 1


def test_late_joiner_does_not_replay_history(engine):
    a = _coord(engine, "a")

    async def run():
        await a.start()
        await a.publish("ui:needs_refresh", {"n": 1})  # before b exists
        b = _coord(engine, "b")
        got = []
        b.subscribe(lambda topic, payload, origin: got.append(payload["n"]))
        await b.start()
        await asyncio.sleep(0.25)
        await a.publish("ui:needs_refresh", {"n": 2})
        for _ in range(40):
            if got:
                break
            await asyncio.sleep(0.05)
        await a.stop()
        await b.stop()
        return got

    assert asyncio.run(run()) == [2]


def test_prune_drops_old_events_and_dead_instances(engine):
    a = _coord(engine, "a")
    old = datetime.utcnow() - timedelta(hours=2)
    dead = datetime.utcnow() - timedelta(days=1)
    with engine.begin() as conn:
        conn.execute(insert(ClusterEvent).values(topic="t", payload={}, origin="z", created_at=old))
        conn.execute(insert(ClusterInstance).values(
            instance_id="ghost", roles="api", started_at=dead, heartbeat_at=dead, meta={},
        ))
    asyncio.run(a.heartbeat({}))
    asyncio.run(a.publish("t", {"fresh": True}))
    a._prune_sync()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM cluster_events")).scalar() == 1
        ids = [r[0] for r in conn.execute(select(ClusterInstance.instance_id))]
    assert ids == ["a"]


def test_server_info_reports_dialect(engine):
    a = _coord(engine, "a")
    info = asyncio.run(a.server_info())
    assert info["driver"] == "db" and info["dialect"] == "sqlite"
