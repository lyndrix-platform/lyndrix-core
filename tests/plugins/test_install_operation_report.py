"""BG-24: an operation opened for an install that never loads anything.

``POST /api/plugins/install`` opened a cluster operation and queued the install;
for an already-installed plugin the install short-circuited without a report,
and the timeout sweep closed the operation 10 minutes later as
"install failed — no report from <this instance>".
"""
import asyncio
import types

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import db_service
from core.components.plugins.api import plugins_api
from core.components.plugins.logic import operations as ops_mod
from core.components.plugins.logic.models import PluginOperation, PluginOperationInstance
from core.components.plugins.logic.plugin_service import PluginService

URL = "https://github.com/lyndrix-platform/lyndrix-plugin-bg24"
FOLDER = "lyndrix_plugin_bg24"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginOperation.__table__.create(bind=engine)
    PluginOperationInstance.__table__.create(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    tracker = ops_mod.operation_tracker
    monkeypatch.setattr(tracker, "live_instances", lambda: ["a"])
    monkeypatch.setattr(tracker, "instance_id", lambda: "a")
    svc = PluginService()
    svc.plugin_dir = tmp_path
    monkeypatch.setattr(plugins_api, "_plugin_service", lambda: svc)
    return svc, tracker, tmp_path


def _installed(plugin_dir, *, with_entrypoint: bool = True):
    folder = plugin_dir / FOLDER
    folder.mkdir()
    if with_entrypoint:
        (folder / "entrypoint.py").write_text("manifest = {}\n")
    return folder


def _post_install():
    payload = plugins_api.InstallRequest(url=URL, version="latest")
    identity = types.SimpleNamespace(username="admin")
    return plugins_api.install_plugin(payload, BackgroundTasks(), identity)


def test_install_of_an_installed_plugin_is_refused_without_opening_an_operation(env):
    svc, tracker, plugin_dir = env
    _installed(plugin_dir)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_post_install())

    assert exc.value.status_code == 409 and "already installed" in exc.value.detail
    assert tracker.list(active_only=True) == []


def test_second_install_while_one_is_running_is_refused(env):
    svc, tracker, plugin_dir = env

    first = asyncio.run(_post_install())
    assert first["operation_id"]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_post_install())

    assert exc.value.status_code == 409 and "in progress" in exc.value.detail
    assert len(tracker.list(active_only=True)) == 1


def test_short_circuited_install_reports_done_to_a_tracked_operation(env):
    """Belt and braces for callers other than the API (and for a folder that
    appears between the API check and the install lock)."""
    svc, tracker, plugin_dir = env
    op_id = tracker.start(plugin_id=None, repo_name=FOLDER, action="install", version="latest",
                          requested_by="admin")
    _installed(plugin_dir)

    assert asyncio.run(svc.install_plugin(URL, "latest", False)) is True

    op = tracker.get(op_id)
    assert op["status"] == "done"
    assert op["instances"][0]["status"] == "done" and op["instances"][0]["message"] == "already installed"


def test_conflicting_folder_without_entrypoint_reports_failed(env):
    svc, tracker, plugin_dir = env
    op_id = tracker.start(plugin_id=None, repo_name=FOLDER, action="install", version="latest",
                          requested_by="admin")
    _installed(plugin_dir, with_entrypoint=False)

    assert asyncio.run(svc.install_plugin(URL, "latest", False)) is False

    op = tracker.get(op_id)
    assert op["status"] != "running" and op["instances"][0]["status"] == "failed"
    assert "already exists" in op["instances"][0]["message"]
