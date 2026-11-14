"""Login-path and admin-CRUD DB work stays off the event loop (BG-12 #8/#9)."""
import asyncio
import inspect
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.api import permissions_api
from core.components.auth.logic import hashing
from core.components.auth.logic.models import User
from core.components.auth.logic.providers.local import LocalProvider
from core.components.database.logic.db_service import db_instance


def test_local_login_queries_in_a_worker_thread(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    User.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add(User(username="alice", hashed_password="stored-hash-not-real", full_name="Alice", roles=["bot"]))
        s.commit()
    threads: list[bool] = []

    def session_factory():
        threads.append(threading.current_thread() is threading.main_thread())
        return Session()

    monkeypatch.setattr(db_instance, "SessionLocal", session_factory, raising=False)
    monkeypatch.setattr(hashing, "verify_password", lambda hashed, pw: hashed == "stored-hash-not-real" and pw == "pw")
    provider = LocalProvider()

    ok = asyncio.run(provider.authenticate("alice", "pw"))
    assert ok is not None and ok.username == "alice" and ok.roles == ["bot"] and ok.full_name == "Alice"
    assert asyncio.run(provider.authenticate("alice", "wrong")) is None
    assert asyncio.run(provider.authenticate("nobody", "pw")) is None
    assert threads and not any(threads)             # the SELECT never ran on the loop thread


def test_permission_admin_handlers_run_in_the_threadpool():
    """They call sync DB services; ``async def`` would run those on the event loop."""
    handlers = [route.endpoint for route in permissions_api.permissions_router.routes]
    assert handlers
    assert not [h.__name__ for h in handlers if inspect.iscoroutinefunction(h)]
