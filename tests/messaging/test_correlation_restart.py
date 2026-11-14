"""CorrelationStore after a restart + messaging housekeeping loops (BG-11).

The database returns naive datetimes; the reloaded cache used to hold them and
``expire_stale`` raised ``TypeError`` (naive vs aware), which killed the
unguarded expire loop for the rest of the process lifetime.
"""
import asyncio
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.components.messaging import entrypoint as messaging_entrypoint
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


def test_reloaded_actions_are_timezone_aware_and_expire_cleanly(db):
    before = CorrelationStore()
    live, short = uuid4(), uuid4()

    async def create():
        await before.create(live, "lyndrix.plugin.x", "x:resume", {"k": 1}, ttl_seconds=600)
        await before.create(short, "lyndrix.plugin.x", "x:resume", {}, ttl_seconds=1)

    asyncio.run(create())

    restarted = CorrelationStore()
    restarted.init_db()                                   # boot: reload pending rows
    assert set(restarted._cache) == {str(live), str(short)}
    assert all(pa.expires_at.tzinfo is timezone.utc for pa in restarted._cache.values())

    async def later():
        assert await restarted.expire_stale() == 0        # used to raise TypeError
        await asyncio.sleep(1.1)
        await restarted.expire_stale()
        assert set(restarted._cache) == {str(live)}
        assert (await restarted.get(live)).state_snapshot == {"k": 1}

    asyncio.run(later())


class _Ctx:
    """Just enough ModuleContext for ``messaging.entrypoint.setup``."""

    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}
        self.handlers: dict = {}
        self.errors: list[str] = []
        self.log = SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda msg, *a, **k: self.errors.append(msg % a if a else msg),
        )

    def subscribe(self, topic):
        def decorator(callback):
            self.handlers[topic] = callback
            return callback
        return decorator

    def create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks[name] = task
        return task


def test_housekeeping_loops_survive_a_failing_pass(monkeypatch):
    gateway = messaging_entrypoint.messaging_gateway
    calls = {"expire": 0, "cleanup": 0}

    async def failing_expire():
        calls["expire"] += 1
        raise TypeError("can't compare offset-naive and offset-aware datetimes")

    async def failing_cleanup():
        calls["cleanup"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(gateway, "expire_stale_actions", failing_expire)
    monkeypatch.setattr(gateway.stream_bridge, "cleanup_stale", failing_cleanup)
    monkeypatch.setattr(gateway, "register", lambda adapter: None)
    monkeypatch.setattr(messaging_entrypoint, "_EXPIRE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(messaging_entrypoint, "_STREAM_CLEANUP_INTERVAL_SECONDS", 0.01)
    ctx = _Ctx()

    async def run():
        messaging_entrypoint.setup(ctx)
        await asyncio.sleep(0.15)
        tasks = [ctx.tasks["messaging:expire_stale"], ctx.tasks["messaging:stream_cleanup"]]
        assert not any(t.done() for t in tasks)           # still running after failures
        for t in tasks:
            t.cancel()                                    # … and still cancellable
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(t.cancelled() for t in tasks)

    asyncio.run(run())
    assert calls["expire"] >= 2 and calls["cleanup"] >= 2
    assert ctx.errors


def test_db_init_runs_off_the_event_loop(monkeypatch):
    import threading

    gateway = messaging_entrypoint.messaging_gateway
    seen: dict = {}
    monkeypatch.setattr(gateway, "init_db", lambda: seen.setdefault("thread", threading.current_thread()))
    monkeypatch.setattr(gateway, "start_pending_workers", lambda: None)
    monkeypatch.setattr(gateway, "register", lambda adapter: None)
    ctx = _Ctx()

    async def run():
        messaging_entrypoint.setup(ctx)
        await ctx.handlers["db:connected"]({})
        for t in ctx.tasks.values():
            t.cancel()
        await asyncio.gather(*ctx.tasks.values(), return_exceptions=True)

    asyncio.run(run())
    assert seen["thread"] is not threading.main_thread()
