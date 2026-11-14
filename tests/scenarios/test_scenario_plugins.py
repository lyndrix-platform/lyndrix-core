"""Stories about plugin operations in a cluster: a plugin a peer enabled starts
publishing here, a version that failed verification on one instance, and a
plugin reload on the instance that holds a lease and runs a job.

The module manager is exercised for real; downloads/installs are faked at the
``plugin_service.install_plugin`` seam, the UI/static mounts are stubbed."""
import asyncio
import types

import pytest
from scenario_kit import Recorder, eventually
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cluster_harness import make_coordinator, make_service
from core.bus import bus
from core.components.cluster.logic import cluster_service as cs_mod
from core.components.database.logic import db_service
from core.components.plugins.logic import manager as manager_mod
from core.components.plugins.logic import plugin_service as ps_mod
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions

MID = "lyndrix.plugin.x"
FOLDER = "lyndrix_plugin_x"
REPO_URL = "https://github.com/lyndrix-platform/lyndrix-plugin-x"


# --- CL-03: a plugin activated because a peer enabled it can publish ------------------------

def test_plugin_enabled_by_a_peer_starts_publishing_cluster_events_here(engine, clock, ready_core, monkeypatch):
    """Given pod-1 runs the manager with plugin ``demo`` disabled and pod-2 is a peer.
    When pod-2 enables the plugin (a ``plugin:desired_changed`` event through the database)
    and pod-1's reconcile activates it.
    Then the plugin's worker, started inside that peer-triggered reconcile, can emit
    ``cluster=True`` events and a runtime status, and pod-2 receives both (the relay mark
    of the triggering event did not leak into the plugin's life)."""
    pod1 = make_service(engine, "pod-1", clock=clock)
    pod2 = make_coordinator(engine, "pod-2", clock=clock)
    got: list = []
    pod2.subscribe(lambda topic, payload, origin: got.append((topic, payload.get("plugin_id"), origin)))

    mgr = ModuleManager()
    manifest = ModuleManifest(id="demo", name="demo", version="1.0.0", type="PLUGIN",
                              permissions={"emit": ["demo:changed"], "subscribe": []})
    module = types.ModuleType("plugins.demo.entrypoint")

    async def worker(ctx):
        await asyncio.sleep(0.05)
        ctx.emit("demo:changed", {"plugin_id": "demo"}, cluster=True)
        ctx.set_status("degraded", message="warming up")

    module.setup = lambda ctx: ctx.create_task(worker(ctx), name="demo:worker")
    mgr.registry["demo"] = {"manifest": manifest, "module": module, "context": ModuleContext(manifest),
                            "status": "disabled", "persisted": True}
    row = {"module_id": "demo", "is_active": True, "installed_version": "1.0.0", "desired_version": "1.0.0",
           "repo_url": None}
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(mgr, "_load_plugin_states", lambda: [row])
    mgr.attach_bus()

    async def run():
        await pod2.start()
        await pod1._start_once()
        pod1.bridge.attach()
        try:
            await pod2.publish("plugin:desired_changed", {"plugin_id": "demo", "action": "enabled"})
            assert await eventually(lambda: ("demo:changed", "demo", "pod-1") in got
                                    and ("plugin:runtime_status", "demo", "pod-1") in got, timeout=5)
            assert mgr.registry["demo"]["status"] == "active"
        finally:
            pod1.bridge.detach()
            mgr.registry["demo"]["context"].dispose()
            await pod2.stop()
            for topic, handler in (("plugin:files_changed", mgr._handle_plugin_change),
                                   ("plugin:desired_changed", mgr._on_desired_changed),
                                   ("system:boot_complete", mgr._start_cluster_reconciler)):
                bus.unsubscribe(topic, handler)

    asyncio.run(run())


# --- BG-09: a version that failed verification on one instance ------------------------------

def _entry(version, status="active", **extra):
    manifest = types.SimpleNamespace(type="PLUGIN", version=version, id=MID, name="X", repo_url=REPO_URL)
    return {"status": status, "manifest": manifest, "module": None, "context": None, "persisted": True, **extra}


def test_version_that_failed_verification_on_one_instance_is_backed_off_and_reported(monkeypatch):
    """Given the cluster is asked to run 1.4.0; pod-1 already tried it, failed verification
    and reverted to 1.3.0; pod-2 never saw it.
    When the operator's upgrade operation is open and both instances reconcile (repeatedly).
    Then pod-2 installs 1.4.0, pod-1 downloads nothing (not once, however often it reconciles)
    and reports to the operation that 1.4.0 failed verification on it."""
    from core.components.plugins.logic import operations as ops_mod
    from core.components.plugins.logic.models import PluginOperation, PluginOperationInstance

    pod1, pod2 = ModuleManager(), ModuleManager()
    pod1.registry[MID] = _entry("1.3.0", reverted_from="1.4.0")
    pod2.registry[MID] = _entry("1.3.0")
    pod1._reconcile_verify_failed(MID, "1.4.0")
    installs: list = []
    current = {"who": None}

    async def fake_install(url, version="latest", upgrade=False, notify=True, lead=True):
        installs.append((current["who"], version))
        return True

    row = {"module_id": MID, "is_active": True, "installed_version": "1.4.0", "desired_version": "1.4.0",
           "repo_url": REPO_URL}
    monkeypatch.setattr(ps_mod.plugin_service, "install_plugin", fake_install)
    for mgr in (pod1, pod2):
        monkeypatch.setattr(mgr, "_load_plugin_states", lambda: [row])
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginOperation.__table__.create(bind=engine)
    PluginOperationInstance.__table__.create(bind=engine)
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    monkeypatch.setattr(db_service.db_instance, "engine", engine, raising=False)
    tracker = ops_mod.operation_tracker
    monkeypatch.setattr(tracker, "live_instances", lambda: ["pod-1", "pod-2"])
    me = {"id": "pod-1"}
    monkeypatch.setattr(tracker, "instance_id", lambda: me["id"])
    op_id = tracker.start(plugin_id=MID, repo_name=FOLDER, action="upgrade", version="v1.4.0", requested_by="admin")

    async def run():
        for who, mgr in (("pod-2", pod2), ("pod-1", pod1), ("pod-1", pod1), ("pod-1", pod1)):
            current["who"], me["id"] = who, who
            await mgr.reconcile_cluster_state(trigger="peer: installed")

    asyncio.run(run())
    assert installs == [("pod-2", "1.4.0")]
    reports = {i["instance_id"]: i for i in tracker.get(op_id)["instances"]}
    assert reports["pod-1"]["status"] == "failed"
    assert "failed verification on this instance" in reports["pod-1"]["message"]


# --- CL-11: reload of a plugin that holds a lease and runs a job -----------------------------

class _RecordingCoordinator:
    """Wraps pod-1's real coordinator and records the moment a lease is released."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def __getattr__(self, item):
        return getattr(self._inner, item)

    async def release(self, name):
        self._log.append("lease released")
        return await self._inner.release(name)


@pytest.fixture()
def reload_env(engine, clock, ready_core, monkeypatch):
    """pod-1 runs the plugin (its service IS the process-wide cluster service for the
    plugin context), pod-2 wants the same lease."""
    log: list[str] = []
    pod1, pod2 = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)
    pod1.leases._coordinator = _RecordingCoordinator(pod1.leases._coordinator, log)
    monkeypatch.setattr(cs_mod, "cluster_service", pod1)
    mgr = ModuleManager()
    for name in ("_teardown_ui", "_unmount_plugin_statics", "_mount_plugin_statics"):
        monkeypatch.setattr(mgr, name, lambda mid: None)
    monkeypatch.setattr(manager_mod.db_instance, "SessionLocal", None, raising=False)

    def register() -> ModuleContext:
        manifest = ModuleManifest(id="lyndrix.plugin.job_probe", name="probe", version="1.0.0", type="PLUGIN",
                                  permissions=ModulePermissions(emit=[], subscribe=[]))
        state = {"stop": asyncio.Event()}

        async def job():
            try:
                await state["stop"].wait()
                log.append("job finished")
            except asyncio.CancelledError:
                log.append("job cancelled")
                raise

        async def teardown(ctx):
            log.append("teardown started")
            await asyncio.sleep(0.1)                 # a worker draining its in-flight work
            log.append("teardown finished")

        ctx = ModuleContext(manifest)
        module = types.SimpleNamespace(teardown=teardown, setup=lambda c: None, __name__="plugins.job_probe.entrypoint",
                                       __file__="/nonexistent/plugins/job_probe/entrypoint.py")
        mgr.registry[manifest.id] = {"manifest": manifest, "module": module, "context": ctx,
                                     "status": "active", "error_message": None}
        ctx.on_lease("worker", acquired=lambda: (log.append("job started"), ctx.create_task(job(), name="probe:job")))
        return ctx

    yield pod1, pod2, mgr, register, log
    entry = mgr.registry.pop("lyndrix.plugin.job_probe", None)
    if entry:
        entry["context"].dispose()


def test_reload_tears_the_job_down_before_the_lease_moves_to_a_peer(reload_env):
    """Given pod-1's plugin holds ``worker`` and runs a job; pod-2 wants the same lease.
    When the plugin is reloaded on pod-1 (teardown takes 0.1 s) and pod-2 tries to acquire
    during the teardown and again afterwards.
    Then teardown finishes first, then the job is cancelled, then the lease is released; pod-2
    gets nothing while the old incarnation still drains and takes the lease right after."""
    pod1, pod2, mgr, register, log = reload_env
    rec = Recorder()

    async def run():
        ctx = register()
        full = ctx._lease_name("worker")
        rec.watch(pod2, full)
        await pod1._start_once()
        await pod2._start_once()
        await pod1.leases.tick()
        await asyncio.sleep(0)
        assert ctx.holds_lease("worker") and log == ["job started"]

        unload = asyncio.ensure_future(mgr.unload_module_async("lyndrix.plugin.job_probe"))
        await asyncio.sleep(0.03)                    # the teardown is in progress
        await pod2.leases.tick()
        assert not pod2.leases.holds(full) and "lease released" not in log
        assert await unload is True
        await pod2.leases.tick()
        assert pod2.leases.holds(full)

    asyncio.run(run())
    assert log == ["job started", "teardown started", "teardown finished", "job cancelled", "lease released"]
    assert [(who, event) for who, event, _ in rec.log] == [("pod-2", "acquired")]


def test_immediate_reenable_gets_the_lease_back_with_a_new_token(reload_env):
    """Given the plugin on pod-1 holds ``worker`` and runs a job, pod-2 contends.
    When the plugin is disabled and enabled again back to back on pod-1, before pod-2 ticks.
    Then the stop completes first (the enable waits for it), the new incarnation starts its
    own job with a NEW token (never a stale renewal of the old holding), and pod-2 stays out."""
    pod1, pod2, mgr, register, log = reload_env
    tokens: list = []

    async def run():
        ctx = register()
        full = ctx._lease_name("worker")
        await pod1._start_once()
        await pod2._start_once()
        pod2.leases.want(full)
        await pod1.leases.tick()
        await asyncio.sleep(0)
        tokens.append(pod1.leases.token(full))

        assert await mgr.unload_module_async("lyndrix.plugin.job_probe") is True
        ctx = register()                             # the re-enabled plugin wants the lease again
        await pod1.leases.tick()
        await asyncio.sleep(0)
        await pod2.leases.tick()
        tokens.append(pod1.leases.token(full))
        assert pod1.leases.holds(full) and not pod2.leases.holds(full)

    asyncio.run(run())
    assert log[:6] == ["job started", "teardown started", "teardown finished", "job cancelled", "lease released",
                       "job started"]
    assert tokens[1] > tokens[0]
