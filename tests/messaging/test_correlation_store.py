"""CorrelationStore across instances: a pending interactive action created on
instance A is resolvable on instance B (DB fallback), exactly once (conditional
UPDATE), and never after it expired.

Run inside the lyndrix-core runtime:
    pytest tests/messaging/test_correlation_store.py -v
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.components.messaging.logic import correlation as mod
from core.components.messaging.logic.correlation import CorrelationStore
from core.components.messaging.model.pending_action import PendingActionRecord


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PendingActionRecord.__table__.create(bind=engine)
    monkeypatch.setattr(mod.db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(mod.db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    monkeypatch.setattr(mod.db_instance, "is_connected", True, raising=False)
    return engine


def test_action_created_on_a_resolves_once_on_b(db):
    a, b = CorrelationStore(), CorrelationStore()
    cid = uuid4()

    async def run():
        await a.create(cid, "lyndrix.plugin.x", "x:resume", {"k": 1}, ttl_seconds=600)
        assert str(cid) not in b._cache
        seen = await b.get(cid)                       # B never saw it in memory → DB
        assert seen is not None and seen.state_snapshot == {"k": 1}
        won = await b.resolve(cid, None)              # B wins the click
        assert won is not None and won.resume_topic == "x:resume"
        assert await a.resolve(cid, None) is None     # A (cache hit) loses: already resolved
        assert await b.resolve(cid, None) is None     # a second click is dropped
        assert await b.get(cid) is None

    asyncio.run(run())


def test_expired_action_is_not_resolvable(db):
    a = CorrelationStore()
    cid = uuid4()

    async def run():
        await a.create(cid, "lyndrix.plugin.x", "x:resume", {}, ttl_seconds=600)
        with mod.db_instance.SessionLocal() as s:
            rec = s.get(PendingActionRecord, str(cid))
            rec.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            s.commit()
        assert await CorrelationStore().get(cid) is None
        assert await CorrelationStore().resolve(cid, None) is None

    asyncio.run(run())


def test_without_database_the_cache_still_works(monkeypatch):
    monkeypatch.setattr(mod.db_instance, "is_connected", False, raising=False)
    monkeypatch.setattr(mod.db_instance, "SessionLocal", None, raising=False)
    a = CorrelationStore()
    cid = uuid4()

    async def run():
        await a.create(cid, "lyndrix.plugin.x", "x:resume", {}, ttl_seconds=60)
        assert (await a.get(cid)) is not None
        assert (await a.resolve(cid, None)) is not None
        assert (await a.resolve(cid, None)) is None

    asyncio.run(run())
