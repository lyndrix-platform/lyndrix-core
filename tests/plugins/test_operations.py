"""Cluster-wide plugin operations (core 1.8): one operation per operator action,
one row per participating instance, exactly one finished event."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.bus import bus
from core.components.database.logic import db_service
from core.components.plugins.logic import operations as ops_mod
from core.components.plugins.logic.models import PluginOperation, PluginOperationInstance
from core.components.plugins.logic.operations import CHANGED_TOPIC, FINISHED_TOPIC, OperationTracker


@pytest.fixture()
def tracker(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginOperation.__table__.create(bind=engine)
    PluginOperationInstance.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    t = OperationTracker()
    monkeypatch.setattr(t, "live_instances", lambda: ["a", "b"])
    monkeypatch.setattr(t, "instance_id", lambda: "a")
    return t, Session


@pytest.fixture()
def events():
    seen: dict[str, list] = {CHANGED_TOPIC: [], FINISHED_TOPIC: []}

    def _changed(payload):
        seen[CHANGED_TOPIC].append(payload)

    def _finished(payload):
        seen[FINISHED_TOPIC].append(payload)

    _changed.__qualname__ = f"test_ops_changed<{id(seen)}>"
    _finished.__qualname__ = f"test_ops_finished<{id(seen)}>"
    bus.subscribe(CHANGED_TOPIC)(_changed)
    bus.subscribe(FINISHED_TOPIC)(_finished)
    try:
        yield seen
    finally:
        bus.unsubscribe(CHANGED_TOPIC, _changed)
        bus.unsubscribe(FINISHED_TOPIC, _finished)


def test_operation_closes_once_every_instance_reported(tracker, events):
    t, _ = tracker
    op_id = t.start(plugin_id="lyndrix.plugin.x", repo_name="x_plugin", action="upgrade",
                    version="1.2.0", requested_by="admin")
    assert op_id and t.has_running(plugin_id="lyndrix.plugin.x")
    assert events[CHANGED_TOPIC][-1]["total"] == 2 and events[CHANGED_TOPIC][-1]["done"] == 0

    assert t.report(plugin_id="lyndrix.plugin.x", repo_name="x_plugin", status="done") == op_id
    assert events[FINISHED_TOPIC] == []                         # b has not reported yet
    assert t.get(op_id)["status"] == "running" and t.get(op_id)["done"] == 1

    t.report(plugin_id="lyndrix.plugin.x", repo_name=None, status="done", instance_id="b")
    assert len(events[FINISHED_TOPIC]) == 1                    # exactly one
    fin = events[FINISHED_TOPIC][0]
    assert fin["status"] == "done" and fin["done"] == 2 and fin["total"] == 2 and fin["failed"] == []
    assert t.get(op_id)["status"] == "done"
    assert not t.has_running(plugin_id="lyndrix.plugin.x")
    # a late duplicate report attaches to nothing
    assert t.report(plugin_id="lyndrix.plugin.x", repo_name=None, status="done", instance_id="b") is None


def test_failed_and_reverted_participants_make_it_partial_or_failed(tracker, events):
    t, _ = tracker
    op1 = t.start(plugin_id="p.one", repo_name="one", action="upgrade", version="2", requested_by="admin")
    t.report(plugin_id="p.one", repo_name=None, status="done")
    t.report(plugin_id="p.one", repo_name=None, status="reverted", message="health error", instance_id="b")
    fin = events[FINISHED_TOPIC][-1]
    assert fin["operation_id"] == op1 and fin["status"] == "partial"
    assert fin["failed"] == [{"instance_id": "b", "status": "reverted", "message": "health error"}]

    op2 = t.start(plugin_id="p.two", repo_name="two", action="install", version=None, requested_by="admin")
    t.report(plugin_id="p.two", repo_name=None, status="failed", message="boom")
    t.report(plugin_id="p.two", repo_name=None, status="failed", instance_id="b")
    fin = events[FINISHED_TOPIC][-1]
    assert fin["operation_id"] == op2 and fin["status"] == "failed" and fin["done"] == 0
    listing = t.list(limit=10)
    assert [o["id"] for o in listing] == [op2, op1]
    assert t.list(active_only=True) == []


def test_fresh_install_is_matched_by_repo_name_and_learns_the_plugin_id(tracker, events):
    t, _ = tracker
    op_id = t.start(plugin_id=None, repo_name="lyndrix_plugin_new", action="install", version="latest",
                    requested_by="admin")
    # the lead learns the id when the files are loaded; a follower may only know the id
    assert t.report(plugin_id="lyndrix.plugin.new", repo_name="lyndrix_plugin_new", status="done",
                    version="0.1.0") == op_id
    assert t.report(plugin_id="lyndrix.plugin.new", repo_name=None, status="done", instance_id="b") == op_id
    op = t.get(op_id)
    assert op["plugin_id"] == "lyndrix.plugin.new" and op["version"] == "0.1.0" and op["status"] == "done"


def test_unexpected_instance_is_added_and_timeout_closes_stale_operations(tracker, events):
    t, Session = tracker
    op_id = t.start(plugin_id="p.slow", repo_name="slow", action="upgrade", version="3", requested_by="admin")
    t.report(plugin_id="p.slow", repo_name=None, status="done", instance_id="c")   # joined after start
    assert t.get(op_id)["total"] == 3
    with Session() as s:
        op = s.get(PluginOperation, op_id)
        op.requested_at = op.requested_at - timedelta(seconds=3600)
        s.commit()
    assert t.sweep_timeouts(max_age_s=600) == 1
    fin = events[FINISHED_TOPIC][-1]
    assert fin["status"] == "partial" and sorted(fin["pending"]) == ["a", "b"] and fin["done"] == 1
    assert t.sweep_timeouts(max_age_s=600) == 0


def test_report_without_an_operation_is_a_noop(tracker, events):
    t, _ = tracker
    assert t.report(plugin_id="p.none", repo_name="none", status="done") is None
    assert events[FINISHED_TOPIC] == [] and events[CHANGED_TOPIC] == []
    assert ops_mod.operation_tracker.available() in (True, False)   # module singleton importable


def test_instance_already_at_the_requested_version_reports_done(tracker, events, monkeypatch):
    """Re-issued upgrade / files already in place: nothing installs on that
    instance, so the reconcile reports it done instead of leaving it pending."""
    import asyncio
    import types

    from core.components.plugins.logic.manager import ModuleManager

    t, _ = tracker
    monkeypatch.setattr(ops_mod, "operation_tracker", t)
    monkeypatch.setattr(t, "instance_id", lambda: "b")
    op_id = t.start(plugin_id="lyndrix.plugin.cur", repo_name="cur", action="upgrade", version="v1.4.0",
                    requested_by="admin")
    mgr = ModuleManager()
    manifest = types.SimpleNamespace(type="PLUGIN", version="1.4.0", id="lyndrix.plugin.cur", repo_url="https://x/y")
    mgr.registry["lyndrix.plugin.cur"] = {"status": "active", "manifest": manifest, "module": None, "context": None}
    asyncio.run(mgr._report_already_current())
    op = t.get(op_id)
    assert {i["instance_id"]: i["status"] for i in op["instances"]} == {"a": "pending", "b": "done"}
    # a second pass changes nothing; a different loaded version reports nothing
    asyncio.run(mgr._report_already_current())
    manifest.version = "1.3.0"
    asyncio.run(mgr._report_already_current())
    assert t.get(op_id)["done"] == 1


def test_instances_that_left_the_cluster_do_not_block_an_operation(tracker, events, monkeypatch):
    """A rollout replaced a pod after the operation opened: the old pod never
    reports — it is marked gone and the operation closes on the live ones."""
    t, _ = tracker
    op_id = t.start(plugin_id="p.roll", repo_name="roll", action="upgrade", version="2", requested_by="admin")
    monkeypatch.setattr(t, "live_instances", lambda: ["a"])          # b is gone now
    t.report(plugin_id="p.roll", repo_name=None, status="done")        # a reports
    fin = events[FINISHED_TOPIC][-1]
    assert fin["operation_id"] == op_id and fin["status"] == "done" and fin["done"] == 1 and fin["total"] == 1
    op = t.get(op_id)
    assert {i["instance_id"]: i["status"] for i in op["instances"]} == {"a": "done", "b": "gone"}


def test_lead_that_dies_while_installing_does_not_keep_the_operation_open(tracker, events, monkeypatch):
    """CL-14: only ``pending`` participants were ever marked gone — a lead that
    reported ``installing`` and died kept the operation open for the 10-minute
    idle sweep and then closed it as partial."""
    t, Session = tracker
    op_id = t.start(plugin_id="p.dead", repo_name="dead", action="upgrade", version="2", requested_by="admin")
    t.report(plugin_id="p.dead", repo_name=None, status="installing")              # a (lead) starts …
    monkeypatch.setattr(t, "live_instances", lambda: ["b"])                        # … and dies
    monkeypatch.setattr(t, "instance_id", lambda: "b")
    t.report(plugin_id="p.dead", repo_name=None, status="done")                    # b finishes right away
    assert t.get(op_id)["status"] == "running"       # a reported seconds ago: maybe just between heartbeats
    with Session() as s:                              # … a full TTL later it still has not
        row = s.get(PluginOperationInstance, {"operation_id": op_id, "instance_id": "a"})
        row.updated_at = row.updated_at - timedelta(seconds=31)
        s.commit()
    assert t.sweep_timeouts(max_age_s=600) == 1
    fin = events[FINISHED_TOPIC][-1]
    assert fin["operation_id"] == op_id and fin["status"] == "done" and fin["done"] == 1 and fin["total"] == 1
    assert {i["instance_id"]: i["status"] for i in t.get(op_id)["instances"]} == {"a": "gone", "b": "done"}


def test_an_instance_without_a_cluster_view_marks_nobody_gone(tracker, events, monkeypatch):
    """A fresh pod whose registry view is empty used to mark every other
    participant gone on its first report and close the operation as done 1/1."""
    t, _ = tracker
    op_id = t.start(plugin_id="p.blind", repo_name="blind", action="upgrade", version="2", requested_by="admin")
    monkeypatch.setattr(t, "live_instances", lambda: None)                         # view not usable
    monkeypatch.setattr(t, "instance_id", lambda: "c")                             # a third instance reports
    t.report(plugin_id="p.blind", repo_name=None, status="done")
    op = t.get(op_id)
    assert op["status"] == "running" and events[FINISHED_TOPIC] == []
    assert {i["instance_id"]: i["status"] for i in op["instances"]} == {"a": "pending", "b": "pending", "c": "done"}
    # opening an operation without a view starts with this instance only
    other = t.start(plugin_id="p.solo", repo_name="solo", action="install", version="1", requested_by="admin")
    assert [i["instance_id"] for i in t.get(other)["instances"]] == ["c"]


def test_live_instances_is_unknown_while_the_registry_view_is_not_ready(monkeypatch):
    import types

    t = OperationTracker()
    monkeypatch.setattr(t, "instance_id", lambda: "me")
    view = types.SimpleNamespace(is_cluster=True, view_ready=False, _instances=[], ttl=30)
    monkeypatch.setattr(OperationTracker, "_cluster", staticmethod(lambda: view))
    assert t.live_instances() is None
    view.view_ready = True
    view._instances = [types.SimpleNamespace(instance_id="peer", alive=True),
                       types.SimpleNamespace(instance_id="old", alive=False)]
    assert t.live_instances() == ["peer", "me"]
