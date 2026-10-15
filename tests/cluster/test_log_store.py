"""Central log store: the handler buffers, the store ships to cluster_logs, the
API-side queries filter by instance/source/level, the leader prunes.

Run inside the lyndrix-core runtime:
    pytest tests/cluster/test_log_store.py -v
"""
import asyncio
import logging

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.components.cluster.logic import log_store as mod
from core.components.cluster.logic.log_store import ClusterLogHandler, LogStore
from core.components.cluster.logic.models import ClusterLog
from core.components.database.logic.db_service import Base


@pytest.fixture()
def store(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[ClusterLog.__table__])
    monkeypatch.setattr(mod.db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(mod.db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    monkeypatch.setattr(mod.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(mod.db_instance, "advisory_lock", None, raising=False)
    handler = ClusterLogHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.configure(enabled=True, level="INFO", exclude="uvicorn.access,noisy.*")
    s = LogStore(handler)
    s.instance_id = "node-a"
    s._schema_ready = True
    return s


def _rec(name, level, msg):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_handler_filters_level_and_excludes(store):
    h = store.handler
    h.handle(_rec("Core:Boot", logging.INFO, "hello"))
    h.handle(_rec("Core:Boot", logging.DEBUG, "too chatty"))      # below INFO
    h.handle(_rec("uvicorn.access", logging.INFO, "GET /"))         # excluded exact
    h.handle(_rec("noisy.sub", logging.WARNING, "prefix excluded"))  # excluded prefix
    h.handle(_rec("Plugin:X", logging.ERROR, "boom"))
    assert h.pending() == 2
    assert [name for name, *_ in h.drain()] == ["Core:Boot", "Plugin:X"]


def test_flush_ships_lines_and_query_filters(store):
    h = store.handler
    for i in range(5):
        h.handle(_rec("Core:Boot", logging.INFO, f"line {i}"))
    h.handle(_rec("Plugin:X", logging.WARNING, "careful"))
    assert asyncio.run(store.flush()) == 6 and store.shipped == 6
    # another instance's lines
    store.instance_id = "node-b"
    h.handle(_rec("Core:Boot", logging.ERROR, "b failed"))
    asyncio.run(store.flush())

    allrows = store.query(instance="all", limit=100)
    assert [r["instance"] for r in allrows] == ["node-a"] * 6 + ["node-b"]
    assert store.query(instance="node-a", source="Plugin:X")[0]["message"].endswith("careful")
    assert [r["message"] for r in store.query(instance="all", min_level="WARNING")] == \
        ["WARNING Plugin:X careful", "ERROR Core:Boot b failed"]
    tail = store.query(instance="node-a", limit=2)
    assert [r["message"][-6:] for r in tail] == ["line 4", "areful"]
    since = store.query(instance="all", since_id=tail[-1]["id"])
    assert [r["instance"] for r in since] == ["node-b"]
    assert store.sources("node-a") == ["Core:Boot", "Plugin:X"]
    inst = store.instances()
    assert {i["instance_id"]: i["lines"] for i in inst} == {"node-a": 6, "node-b": 1}


def test_prune_keeps_cap_per_instance(store):
    store.max_per_instance = 3
    h = store.handler
    for i in range(10):
        h.handle(_rec("Core:Boot", logging.INFO, f"a{i}"))
    asyncio.run(store.flush())
    store.instance_id = "node-b"
    h.handle(_rec("Core:Boot", logging.INFO, "b0"))
    asyncio.run(store.flush())
    removed = store.prune()
    assert removed == 7
    with mod.db_instance.SessionLocal() as s:
        rows = s.execute(select(ClusterLog.instance_id, ClusterLog.message).order_by(ClusterLog.id)).all()
    assert [m for _, m in rows] == ["INFO Core:Boot a7", "INFO Core:Boot a8", "INFO Core:Boot a9", "INFO Core:Boot b0"]


def test_disconnected_db_drops_instead_of_blocking(store, monkeypatch):
    monkeypatch.setattr(mod.db_instance, "is_connected", False, raising=False)
    store.handler.handle(_rec("Core:Boot", logging.INFO, "lost"))
    assert asyncio.run(store.flush()) == 0 and store.dropped == 1 and store.handler.pending() == 0
