"""BG-09 (cluster): a version that failed verification here is not retried on
every reconcile pass.

The row keeps the version the cluster was asked to run (a local revert never
writes ``plugin_states.installed_version`` — one writer, the operator's lead
instance), so the reconcile used to download → swap → verify → revert the
failing version again on every pass. The failure is remembered per
(plugin, version) in memory instead.
"""
import asyncio
import types

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import db_service
from core.components.plugins.logic import plugin_service as ps_mod
from core.components.plugins.logic.manager import ModuleManager

MID = "lyndrix.plugin.x"
FOLDER = "lyndrix_plugin_x"
REPO_URL = "https://github.com/lyndrix-platform/lyndrix-plugin-x"


def _entry(version: str, status: str = "active", **extra) -> dict:
    manifest = types.SimpleNamespace(type="PLUGIN", version=version, id=MID, name="X", repo_url=REPO_URL)
    return {"status": status, "manifest": manifest, "module": None, "context": None, "persisted": True, **extra}


def _cluster_env(monkeypatch, mgr, *, row_version: str):
    calls = []

    async def fake_install(url, version="latest", upgrade=False, notify=True, lead=True):
        calls.append(version)
        return True

    monkeypatch.setattr(ps_mod.plugin_service, "install_plugin", fake_install)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    row = {"module_id": MID, "is_active": True, "installed_version": row_version,
           "desired_version": row_version, "repo_url": REPO_URL}
    monkeypatch.setattr(mgr, "_load_plugin_states", lambda: [row])
    return calls


def test_failed_verification_backs_the_version_off_in_the_reconcile(monkeypatch):
    mgr = ModuleManager()
    mgr.registry[MID] = _entry("1.3.0")
    calls = _cluster_env(monkeypatch, mgr, row_version="1.4.0")
    row_writes = []

    # The install handler: 1.4.0 loads, fails verification, is reverted to 1.3.0.
    async def fake_reload(module_id, module_folder=None):
        mgr.registry[MID] = _entry("1.4.0", status="failed")
        return True

    async def fake_verify(module_id):
        return False

    async def fake_revert(module_name, module_id):
        mgr.registry[MID] = _entry("1.3.0", reverted_from="1.4.0")
        return True

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(mgr, "reload_module", fake_reload)
    monkeypatch.setattr(mgr, "_verify_after_install", fake_verify)
    monkeypatch.setattr(mgr, "_auto_revert", fake_revert)
    monkeypatch.setattr(mgr, "_activate_saved_plugins", _noop)
    monkeypatch.setattr(mgr, "_report_operation", _noop)
    monkeypatch.setattr(mgr, "_find_plugin_id_by_folder", lambda name: MID)
    monkeypatch.setattr(mgr, "_read_plugin_manifest_id", lambda name: MID)
    monkeypatch.setattr(mgr, "_read_folder_version", lambda name: "1.4.0")
    monkeypatch.setattr(mgr, "_persist_plugin_state", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "_write_row_version", lambda mid, version: row_writes.append(version))

    async def run():
        # follower install (cluster reconcile) of the version the row asks for
        await mgr._handle_plugin_install({"action": "install", "name": FOLDER, "lead": False}, FOLDER)
        assert row_writes == []                       # a follower never writes the row

        first = await mgr.reconcile_cluster_state(trigger="periodic")
        second = await mgr.reconcile_cluster_state(trigger="peer")
        assert calls == [] and not first.get("upgraded") and not second.get("upgraded")

        # … the back-off expires → one retry
        key = mgr._verify_backoff_key(MID, "v1.4.0")
        failures, _next = mgr._reconcile_verify_backoff[key]
        mgr._reconcile_verify_backoff[key] = (failures, 0.0)
        third = await mgr.reconcile_cluster_state(trigger="periodic")
        assert calls == ["1.4.0"] and third.get("upgraded") == [MID]

    asyncio.run(run())


def test_reconcile_skips_a_version_this_instance_reverted_from(monkeypatch):
    """``reverted_from == want`` alone is enough — also when the back-off entry
    was never recorded (revert through another path)."""
    mgr = ModuleManager()
    mgr.registry[MID] = _entry("1.3.0", reverted_from="1.4.0")
    calls = _cluster_env(monkeypatch, mgr, row_version="v1.4.0")
    summary = asyncio.run(mgr.reconcile_cluster_state(trigger="periodic"))
    assert calls == [] and not summary.get("upgraded")
    assert mgr._reconcile_verify_allowed(MID, "1.4.0") is False

    # a different target version (the operator moved on) is installed right away
    calls = _cluster_env(monkeypatch, mgr, row_version="1.4.1")
    summary = asyncio.run(mgr.reconcile_cluster_state(trigger="periodic"))
    assert calls == ["1.4.1"] and summary.get("upgraded") == [MID]


def test_successful_verification_clears_the_backoff(monkeypatch):
    mgr = ModuleManager()
    mgr._reconcile_verify_failed(MID, "1.4.0")
    mgr._reconcile_verify_failed("lyndrix.plugin.other", "2.0.0")
    mgr._reconcile_verify_cleared(MID)
    assert mgr._reconcile_verify_allowed(MID, "1.4.0") is True
    assert mgr._reconcile_verify_allowed("lyndrix.plugin.other", "2.0.0") is False


# --- a re-issued operation gets an answer from a backed-off instance -------------

def _tracked_env(monkeypatch, mgr):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from core.components.plugins.logic import operations as ops_mod
    from core.components.plugins.logic.models import PluginOperation, PluginOperationInstance

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginOperation.__table__.create(bind=engine)
    PluginOperationInstance.__table__.create(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    tracker = ops_mod.operation_tracker
    monkeypatch.setattr(tracker, "live_instances", lambda: ["me", "peer"])
    monkeypatch.setattr(tracker, "instance_id", lambda: "me")
    return tracker


def test_backed_off_instance_reports_to_a_reissued_operation_at_once(monkeypatch):
    mgr = ModuleManager()
    mgr.registry[MID] = _entry("1.3.0", reverted_from="1.4.0")
    mgr._reconcile_verify_failed(MID, "1.4.0")
    calls = _cluster_env(monkeypatch, mgr, row_version="1.4.0")
    tracker = _tracked_env(monkeypatch, mgr)
    monkeypatch.setattr(mgr, "_load_plugin_states", lambda: [
        {"module_id": MID, "is_active": True, "installed_version": "1.4.0",
         "desired_version": "1.4.0", "repo_url": REPO_URL}])

    # The operator re-issues the upgrade on a peer; this instance reconciles.
    op_id = tracker.start(plugin_id=MID, repo_name=FOLDER, action="upgrade", version="v1.4.0",
                          requested_by="admin")
    asyncio.run(mgr.reconcile_cluster_state(trigger="peer peer-1: installed"))

    assert calls == []                                    # still backed off: no download
    mine = next(i for i in tracker.get(op_id)["instances"] if i["instance_id"] == "me")
    assert mine["status"] == "failed"
    assert "failed verification on this instance" in mine["message"] and "min" in mine["message"]


def test_no_premature_report_while_this_instance_is_installing(monkeypatch):
    """The lead of the re-issued operation is itself backed off from an earlier
    attempt: its own install is in flight and reports by itself."""
    mgr = ModuleManager()
    mgr.registry[MID] = _entry("1.3.0", reverted_from="1.4.0")
    mgr._reconcile_verify_failed(MID, "1.4.0")
    _cluster_env(monkeypatch, mgr, row_version="1.4.0")
    tracker = _tracked_env(monkeypatch, mgr)
    op_id = tracker.start(plugin_id=MID, repo_name=FOLDER, action="upgrade", version="1.4.0",
                          requested_by="admin")

    async def run():
        async with ps_mod.plugin_service._install_lock(FOLDER):      # download in progress here
            await mgr._report_already_current()

    asyncio.run(run())
    mine = next(i for i in tracker.get(op_id)["instances"] if i["instance_id"] == "me")
    assert mine["status"] == "pending"


def test_operation_for_another_version_is_not_answered_by_the_backoff(monkeypatch):
    mgr = ModuleManager()
    mgr.registry[MID] = _entry("1.3.0", reverted_from="1.4.0")
    mgr._reconcile_verify_failed(MID, "1.4.0")
    _cluster_env(monkeypatch, mgr, row_version="1.4.1")
    tracker = _tracked_env(monkeypatch, mgr)
    op_id = tracker.start(plugin_id=MID, repo_name=FOLDER, action="upgrade", version="1.4.1",
                          requested_by="admin")
    asyncio.run(mgr._report_already_current())
    mine = next(i for i in tracker.get(op_id)["instances"] if i["instance_id"] == "me")
    assert mine["status"] == "pending"                   # the reconcile installs 1.4.1 and reports then
