"""BG-08: the automatic revert after a failed upgrade verification must announce
itself.

``_auto_revert`` returned before its announcements — no ``plugin:rolled_back`` /
``plugin:install_failed`` / ``ui:needs_refresh`` — and wrote its history row with
a sync DB call on the event loop.
"""
import asyncio
import threading
import types

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import db_service
from core.components.plugins.logic import manager as manager_mod
from core.components.plugins.logic import plugin_service as ps_mod
from core.components.plugins.logic.manager import ModuleManager

MID = "lyndrix.plugin.x"
FOLDER = "lyndrix_plugin_x"
REPO_URL = "https://github.com/lyndrix-platform/lyndrix-plugin-x"


def _entry(version: str, status: str = "active", **extra) -> dict:
    manifest = types.SimpleNamespace(type="PLUGIN", version=version, id=MID, name="X", repo_url=REPO_URL)
    return {"status": status, "manifest": manifest, "module": None, "context": None, "persisted": True, **extra}



def test_auto_revert_announces_the_revert_and_records_history_off_loop(monkeypatch):
    mgr = ModuleManager()
    mgr.registry[MID] = _entry("2.0.0", status="failed")
    emitted, history = [], []
    loop_thread = threading.get_ident()

    svc = ps_mod.plugin_service
    monkeypatch.setattr(svc, "has_previous", lambda name: True)
    monkeypatch.setattr(svc, "previous_version", lambda name: "1.0.0")
    monkeypatch.setattr(svc, "restore_previous", lambda name: True)
    monkeypatch.setattr(
        svc, "_record_history",
        lambda *a, **k: history.append((a, k, threading.get_ident() != loop_thread)),
    )
    monkeypatch.setattr(db_service.db_instance, "is_connected", False, raising=False)
    monkeypatch.setattr(mgr, "unload_module", lambda mid: mgr.registry.pop(mid, None) is not None)

    def fake_load(name, is_plugin=True):
        mgr.registry[MID] = _entry("1.0.0")
        return True

    monkeypatch.setattr(mgr, "load_module", fake_load)
    monkeypatch.setattr(mgr, "_find_plugin_id_by_folder", lambda name: MID if MID in mgr.registry else None)
    monkeypatch.setattr(manager_mod.bus, "emit", lambda topic, payload=None, **kw: emitted.append((topic, payload)))

    assert asyncio.run(mgr._auto_revert(FOLDER, MID)) is True

    topics = [t for t, _ in emitted]
    assert topics == ["plugin:rolled_back", "plugin:install_failed", "plugin:state_changed", "ui:needs_refresh"]
    assert dict(emitted)["plugin:rolled_back"] == {"repo": FOLDER, "module_id": MID, "version": "1.0.0"}
    assert dict(emitted)["plugin:state_changed"] == {"plugin_id": MID, "action": "reverted"}
    # the cluster's version is remembered locally, the history row is written in a thread
    assert mgr.registry[MID]["reverted_from"] == "2.0.0"
    assert len(history) == 1 and history[0][1]["outcome"] == "reverted" and history[0][2] is True


def test_auto_revert_without_a_previous_version_announces_nothing(monkeypatch):
    mgr = ModuleManager()
    emitted = []
    monkeypatch.setattr(ps_mod.plugin_service, "has_previous", lambda name: False)
    monkeypatch.setattr(manager_mod.bus, "emit", lambda topic, payload=None, **kw: emitted.append(topic))
    assert asyncio.run(mgr._auto_revert(FOLDER, MID)) is False
    assert emitted == []
