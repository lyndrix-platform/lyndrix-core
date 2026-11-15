"""Instance lifecycle (CL-08, CL-07): a graceful restart is "restarting", not
"gone"; a restart under a fixed id is a new incarnation; SIGTERM drains at
once; plugins can ask what became of an instance.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, text, update

from cluster_harness import ClusterClock, make_coordinator, make_engine, make_service
from core.api.router_registry import RUNTIME_GATE_HEADER, _remote_router
from core.bus import bus
from core.components.cluster.logic import cluster_service as cs_mod
from core.components.cluster.logic.coordinator import InstanceIdConflict
from core.components.cluster.logic.drain import DrainController
from core.components.cluster.logic.models import ClusterInstance
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.models import ModuleManifest
from core.services import boot_service, db_instance, vault_instance

TTL, GRACE = 30, 90


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(tmp_path)
    yield eng
    eng.dispose()


@pytest.fixture()
def ready_core(monkeypatch):
    """Boot finished, database and Vault connected (process-global flags)."""
    from core.components.boot.logic.boot_service import BootPhase

    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)


@pytest.fixture()
def lost_events():
    """``cluster:instance_lost`` as observed by pod-1 (every simulated instance
    emits on the one process-wide bus; the payload names the observer)."""
    seen: list = []

    def _lost(payload):
        if payload.get("observed_by") == "pod-1":
            seen.append(payload)

    _lost.__qualname__ = f"lifecycle_test_lost<{id(seen)}>"
    bus.subscribe(cs_mod.INSTANCE_LOST_TOPIC)(_lost)
    yield seen
    bus.unsubscribe(cs_mod.INSTANCE_LOST_TOPIC, _lost)


def _view(instances):
    return {i.instance_id: i for i in instances}


# --- registry rows -------------------------------------------------------------------

def test_graceful_stop_is_restarting_within_the_grace_then_dead(engine):
    clock = ClusterClock()
    api, runner = make_coordinator(engine, "pod-1", clock=clock), make_coordinator(engine, "docker-auto-runner", clock=clock, roles=["runner"])

    async def run():
        await api.start(), await runner.start()
        await api.heartbeat({}), await runner.heartbeat({})
        assert await runner.acquire("iac:webhook_sync") is not None
        assert _view(await api.instances())["docker-auto-runner"].state == "alive"
        await runner.stop()                                   # graceful: tombstone, leases freed
        seen = _view(await api.instances())["docker-auto-runner"]
        assert (seen.state, seen.alive) == ("restarting", False) and seen.stopped_at is not None
        assert seen.roles == ["runner"] and await api.holder("iac:webhook_sync") is None
        clock.advance(GRACE - 1)
        assert _view(await api.instances())["docker-auto-runner"].state == "restarting"
        clock.advance(2)
        assert _view(await api.instances())["docker-auto-runner"].state == "dead"

    asyncio.run(run())


def test_crash_is_alive_for_a_ttl_then_dead_but_a_killed_drain_is_restarting(engine):
    clock = ClusterClock()
    api = make_coordinator(engine, "pod-1", clock=clock)
    crashed, killed = make_coordinator(engine, "pod-2", clock=clock), make_coordinator(engine, "docker-auto", clock=clock)

    async def states():
        return {i.instance_id: i.state for i in await api.instances()}

    async def scenario():
        await crashed.heartbeat({"draining": False})
        await killed.heartbeat({"draining": True})            # announced the drain, then SIGKILL (no tombstone)
        first = await states()
        clock.advance(TTL + 1)
        after_ttl = await states()
        clock.advance(GRACE)
        after_grace = await states()
        return first, after_ttl, after_grace

    first, after_ttl, after_grace = asyncio.run(scenario())
    assert first == {"pod-2": "alive", "docker-auto": "draining"}
    assert after_ttl == {"pod-2": "dead", "docker-auto": "restarting"}
    assert after_grace == {"pod-2": "dead", "docker-auto": "dead"}


def test_restart_under_a_fixed_id_is_a_new_incarnation(engine):
    clock = ClusterClock()
    api = make_coordinator(engine, "pod-1", clock=clock)

    async def life(stop: bool):
        coord = make_coordinator(engine, "docker-auto-runner", clock=clock, roles=["runner"])
        await coord.start()
        await coord.heartbeat({})
        seen = _view(await api.instances())["docker-auto-runner"]
        if stop:
            await coord.stop()
        return coord.incarnation, seen

    first, seen_first = asyncio.run(life(stop=True))
    clock.advance(20)
    second, seen_second = asyncio.run(life(stop=False))
    assert first != second
    assert seen_first.incarnation == first and seen_second.incarnation == second
    assert seen_second.state == "alive" and seen_second.stopped_at is None       # tombstone cleared
    assert seen_second.started_at > seen_first.started_at                        # not the old process's uptime


def test_two_processes_with_one_id_the_older_one_stands_back(engine):
    clock = ClusterClock()
    old = make_coordinator(engine, "core-a", clock=clock)
    new = make_coordinator(engine, "core-a", clock=clock)       # copied compose file: same LYNDRIX_INSTANCE_ID

    async def run():
        await old.start()
        await old.heartbeat({})
        clock.advance(5)
        await new.start()
        await new.heartbeat({})                                 # first beat of a new process: takes the row
        clock.advance(5)
        with pytest.raises(InstanceIdConflict) as err:
            await old.heartbeat({})                             # … the older one notices and does not write
        assert "LYNDRIX_INSTANCE_ID" in str(err.value)
        row = _view(await new.instances())["core-a"]
        assert row.incarnation == new.incarnation
        await new.heartbeat({})                                 # the newer keeps its row without complaint
        await new.stop()
        clock.advance(1)
        await old.heartbeat({})                                 # the other is gone → the older one is back
        assert _view(await old.instances())["core-a"].incarnation == old.incarnation

    asyncio.run(run())


def test_rows_and_writes_of_core_1_11_instances_stay_valid(engine):
    clock = ClusterClock()
    api = make_coordinator(engine, "pod-1", clock=clock)

    async def run():
        with engine.begin() as conn:                            # a 1.11 instance registers (no new columns)
            conn.execute(text(
                "INSERT INTO cluster_instances (instance_id, hostname, roles, started_at, heartbeat_at, meta) "
                "VALUES ('old-pod', 'h', 'api,leader', :now, :now, '{}')"), {"now": clock.db.now})
        old = _view(await api.instances())["old-pod"]
        assert (old.state, old.alive, old.incarnation) == ("alive", True, None)
        # a tombstone of a new instance is revived by a 1.11 process using the same id:
        runner = make_coordinator(engine, "docker-auto-runner", clock=clock, roles=["runner"])
        await runner.start(), await runner.heartbeat({})
        await runner.stop()
        clock.advance(10)
        with engine.begin() as conn:                            # 1.11 heartbeat: UPDATE without stopped_at
            conn.execute(update(ClusterInstance).where(ClusterInstance.instance_id == "docker-auto-runner")
                         .values(heartbeat_at=clock.db.now, meta={}))
        assert _view(await api.instances())["docker-auto-runner"].state == "alive"

    asyncio.run(run())


def test_schema_upgrade_adds_the_instance_columns(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE cluster_instances (instance_id VARCHAR(100) PRIMARY KEY, hostname VARCHAR(255), "
            "roles VARCHAR(100) NOT NULL, core_version VARCHAR(32), api_version VARCHAR(32), "
            "started_at DATETIME NOT NULL, heartbeat_at DATETIME NOT NULL, meta JSON)"))
    coord = make_coordinator(engine, "pod-1")
    asyncio.run(coord.heartbeat({}))
    assert _view(asyncio.run(coord.instances()))["pod-1"].incarnation == coord.incarnation


# --- what plugins see (ClusterService) ------------------------------------------------

def _ctx():
    return ModuleContext(ModuleManifest(id="lyndrix.plugin.lifecycle_test", name="t", version="0", type="PLUGIN"))


def test_runner_restart_has_no_gap_and_a_dead_runner_is_announced_once(engine, ready_core, lost_events):
    """The mechanism behind "No runner instance online" after every core
    rollout: the runner's graceful stop deleted its row at once."""
    clock = ClusterClock()
    api = make_service(engine, "pod-1", clock=clock)

    async def start_runner():
        runner = make_service(engine, "docker-auto-runner", clock=clock, roles=["runner"])
        await runner._start_once()
        return runner

    async def run():
        await api._start_once()
        runner = await start_runner()
        await api._heartbeat_once()
        assert api.role_state("runner")["state"] == "alive"
        first_incarnation = api.instance_state("docker-auto-runner")["incarnation"]

        await runner.stop()                                     # compose recreate: graceful stop …
        await api._heartbeat_once()
        assert api.role_state("runner") == {"role": "runner", "state": "restarting",
                                            "instances": {"docker-auto-runner": "restarting"}}
        assert api.instance_state("docker-auto-runner")["state"] == "restarting"
        assert lost_events == []                                # … is not a loss

        clock.advance(40)                                       # … the new container registers
        runner = await start_runner()
        await api._heartbeat_once()
        state = api.instance_state("docker-auto-runner")
        assert state["state"] == "alive" and state["incarnation"] != first_incarnation
        assert lost_events == []

        clock.advance(TTL + 1)                                  # now it crashes: no goodbye, no heartbeat
        await api._heartbeat_once()
        assert api.role_state("runner")["state"] == "dead"
        await api._heartbeat_once()                             # … announced once, not per beat
        assert [(e["instance_id"], e["graceful"], e["roles"]) for e in lost_events] == [
            ("docker-auto-runner", False, ["runner"])]
        assert lost_events[0]["incarnation"] == state["incarnation"] and lost_events[0]["last_heartbeat"]

        runner = await start_runner()                           # back again, then a planned stop without return
        await api._heartbeat_once()
        await runner.stop()
        await api._heartbeat_once()
        clock.advance(GRACE + 1)
        await api._heartbeat_once()
        assert len(lost_events) == 2 and lost_events[1]["graceful"] is True
        await api.stop()

    asyncio.run(run())


def test_plugin_api_answers_for_roles_and_instances(engine, ready_core, monkeypatch):
    clock = ClusterClock()
    api = make_service(engine, "pod-1", clock=clock)
    monkeypatch.setattr(ModuleContext, "_cluster", staticmethod(lambda: api))
    ctx = _ctx()

    async def run():
        # before the first refresh: this instance does not know — and says so
        assert ctx.cluster_instances == [] and ctx.role_state("runner")["state"] == "unknown"
        assert ctx.has_live_role("runner") is True              # unknown is not "gone"
        assert ctx.instance_state("docker-auto-runner")["state"] == "unknown"
        await api._start_once()
        assert ctx.has_live_role("runner") is False             # now it knows: nobody has the role
        assert ctx.instance_state("docker-auto-runner") == {
            "instance_id": "docker-auto-runner", "state": "dead", "known": False, "roles": [],
            "incarnation": None, "last_heartbeat": None, "age_s": None}
        runner = make_service(engine, "docker-auto-runner", clock=clock, roles=["runner"])
        await runner._start_once()
        await api._heartbeat_once()
        mine = [i for i in ctx.cluster_instances if i["is_self"]][0]
        assert mine["state"] == "alive" and mine["incarnation"] == ctx.incarnation == api.incarnation
        assert ctx.has_live_role("runner") and ctx.instance_state("pod-1")["state"] == "alive"
        await runner.stop()
        await api._heartbeat_once()
        assert ctx.has_live_role("runner") is True              # restarting: expected back
        assert {i["instance_id"]: i["state"] for i in ctx.cluster_instances}["docker-auto-runner"] == "restarting"
        clock.advance(TTL + 11 + 1)                             # the view of pod-1 itself goes stale
        assert ctx.cluster_instances == [] and ctx.instance_state("docker-auto-runner")["state"] == "unknown"
        await api.stop()

    asyncio.run(run())


def test_a_row_deleted_by_a_core_1_11_graceful_stop_counts_as_restarting(engine, ready_core, lost_events):
    clock = ClusterClock()
    api = make_service(engine, "pod-1", clock=clock)

    async def run():
        await api._start_once()
        runner = make_coordinator(engine, "docker-auto-runner", clock=clock, roles=["runner"])
        await runner.heartbeat({})
        await api._heartbeat_once()
        with engine.begin() as conn:                            # what core 1.11 does on a graceful stop
            conn.execute(delete(ClusterInstance).where(ClusterInstance.instance_id == "docker-auto-runner"))
        clock.advance(10)
        await api._heartbeat_once()
        assert api.role_state("runner")["state"] == "restarting" and lost_events == []
        clock.advance(GRACE + 1)
        await api._heartbeat_once()
        assert api.role_state("runner")["state"] == "dead" and len(lost_events) == 1
        await api.stop()

    asyncio.run(run())


def test_id_conflict_takes_the_older_process_out_of_rotation(engine, ready_core):
    clock = ClusterClock()
    old, new = make_service(engine, "core-a", clock=clock), make_service(engine, "core-a", clock=clock)
    events: list = []

    async def run():
        await old._start_once()
        old.leases.want("x", acquired=lambda: events.append("acquired"), lost=lambda: events.append("lost"))
        await old.leases.tick()
        assert old.readiness() == (True, []) and old.is_leader
        clock.advance(5)
        await new._start_once()
        clock.advance(5)
        await old._heartbeat_once()
        assert old.readiness() == (False, ["instance_id_conflict"])
        assert events == ["acquired", "lost"] and old.leases.held == [] and not old.is_leader
        assert "LYNDRIX_INSTANCE_ID" in old.snapshot()["instance"]["id_conflict"]
        await new.stop()
        clock.advance(1)
        await old._heartbeat_once()                             # the duplicate is gone
        assert old.readiness() == (True, []) and old.id_conflict is None
        await old.stop()

    asyncio.run(run())


# --- drain (CL-07) --------------------------------------------------------------------

def test_sigterm_drains_at_once_and_hands_leases_over_lost_first(engine, ready_core):
    """p1: for the whole drain /api/ready said 200, peers never saw 'draining',
    and the leases were released at the end, before the lost callbacks ran."""
    clock = ClusterClock()
    svc, peer = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)
    order: list = []

    async def stop_work():
        order.append("lost:start")
        await asyncio.sleep(0.05)
        order.append("lost:done")

    async def run():
        await svc._start_once()
        await peer._start_once()
        svc.leases.want("monitoring:prober", lost=stop_work)
        await svc.leases.tick()
        assert svc.is_leader and svc.readiness() == (True, [])
        release = svc.coordinator.release

        async def recording_release(name):
            order.append(f"release:{name}")
            return await release(name)

        svc.coordinator.release = recording_release
        drain = DrainController(seconds=1, on_drain=svc._on_drain, on_drain_start=svc.begin_drain)
        drain._forward = lambda: order.append("forwarded")      # do not re-raise SIGTERM in the test
        drain._on_sigterm()
        assert svc.readiness()[1] == ["draining"]               # immediately, not after the sleep
        await asyncio.sleep(0.3)                                # well inside the 1 s drain
        await peer._heartbeat_once()
        seen = peer.instance_state("pod-1")
        assert seen["state"] == "draining" and svc.leases.held == [] and not svc.is_leader
        assert order[:2] == ["lost:start", "lost:done"]         # the worker stopped …
        assert sorted(order[2:]) == ["release:core:leader", "release:monitoring:prober"]   # … then the hand-over
        await peer.leases.tick()
        assert peer.is_leader                                   # took over during the drain
        await asyncio.sleep(1.2)                                # drain over → tombstone → forward
        await peer._heartbeat_once()
        assert order[-1] == "forwarded" and peer.instance_state("pod-1")["state"] == "restarting"
        await svc.stop("shutdown")                              # uvicorn's shutdown hook: idempotent
        await peer.stop()

    asyncio.run(run())
    assert order.index("lost:done") < min(i for i, o in enumerate(order) if o.startswith("release"))


# --- pinned plugins: retryable "role restarting" ---------------------------------------

def test_remote_stub_tells_restarting_from_gone(monkeypatch):
    stub = _remote_router("lyndrix.plugin.iac_orchestrator", "runner").routes[-1].endpoint

    def answer(state):
        monkeypatch.setattr(cs_mod.cluster_service, "role_state", lambda role: {"role": role, "state": state})
        with pytest.raises(HTTPException) as err:
            asyncio.run(stub("jobs"))
        header = json.loads(err.value.headers[RUNTIME_GATE_HEADER])
        assert err.value.status_code == 503 and header["phase"] == "remote" and header["role"] == "runner"
        assert header["blocking"] is True and header["plugin_id"] == "lyndrix.plugin.iac_orchestrator"
        return header["role_state"], err.value.headers["Retry-After"], err.value.detail

    assert answer("alive")[:2] == ("alive", "30")
    state, retry, detail = answer("restarting")
    assert (state, retry) == ("restarting", "5") and "restarting" in detail
    assert answer("draining")[1] == "5"
    state, retry, detail = answer("dead")
    assert (state, retry) == ("dead", "30") and "no such instance" in detail


# --- admin release of a lease a live peer holds -----------------------------------------

def test_admin_release_asks_a_live_holder_to_hand_over(engine, ready_core, monkeypatch):
    """Freeing the row behind a live holder's back left it working until its
    next renewal while another instance already started."""
    clock = ClusterClock()
    holder, other = make_service(engine, "pod-1", clock=clock), make_service(engine, "pod-2", clock=clock)
    asked: list = []
    events: list = []

    def hook(topic, payload, forced):
        if topic == cs_mod.LEASE_HANDOVER_TOPIC:
            asked.append((dict(payload), forced))

    async def run():
        await holder._start_once()
        holder.leases.want("monitoring:prober", acquired=lambda: events.append("acquired"),
                           lost=lambda: events.append("lost"))
        await holder.leases.tick()
        await other._start_once()
        previous = bus._cluster_publisher
        bus.set_cluster_publisher(hook)
        try:
            assert await other.force_release("monitoring:prober") is True
        finally:
            bus.set_cluster_publisher(previous)
        assert asked == [({"name": "monitoring:prober", "holder": "pod-1"}, True)]
        assert (await other.coordinator.holder("monitoring:prober")).holder == "pod-1"   # not yanked away
        await holder._on_handover_requested({**asked[0][0], "_origin": "pod-2"})        # the relayed request
        assert events == ["acquired", "lost"]
        assert await other.coordinator.holder("monitoring:prober") is None
        # force=true (or a dead holder) frees the row directly
        await holder.leases.tick()
        assert await other.force_release("monitoring:prober", force=True) is True
        assert await other.coordinator.holder("monitoring:prober") is None
        await holder.stop(), await other.stop()

    asyncio.run(run())


# --- N3: a heartbeat must not undo this process's own tombstone -----------------------

def test_a_heartbeat_still_under_way_does_not_overwrite_the_tombstone(engine):
    """stop() commits the tombstone while a heartbeat of the same process is
    between its SELECT and its UPDATE. The late UPDATE used to set
    ``stopped_at = NULL`` again: the peers saw the stopped instance as "alive"
    for a lease TTL instead of "restarting"."""
    from sqlalchemy import event

    clock = ClusterClock()
    api = make_coordinator(engine, "pod-1", clock=clock)
    runner = make_coordinator(engine, "docker-auto-runner", clock=clock, roles=["runner"])
    armed = {"on": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _stop_commits_first(conn, cursor, statement, parameters, context, executemany):
        if armed["on"] and statement.lstrip().upper().startswith("UPDATE CLUSTER_INSTANCES"):
            armed["on"] = False
            runner._deregister_sync()            # the stop: tombstone committed on another connection

    async def run():
        await runner.start()
        await runner.heartbeat({"ready": True})
        armed["on"] = True
        await runner.heartbeat({"ready": True})  # SELECT → (tombstone commits) → UPDATE
        assert armed["on"] is False
        return _view(await api.instances())["docker-auto-runner"]

    seen = asyncio.run(run())
    event.remove(engine, "before_cursor_execute", _stop_commits_first)
    assert (seen.state, seen.alive) == ("restarting", False) and seen.stopped_at is not None


def test_a_heartbeat_after_stop_does_not_revive_but_a_restart_does(engine):
    clock = ClusterClock()
    api = make_coordinator(engine, "pod-1", clock=clock)
    runner = make_coordinator(engine, "docker-auto-runner", clock=clock, roles=["runner"])

    async def state():
        return _view(await api.instances())["docker-auto-runner"].state

    async def run():
        await runner.start()
        await runner.heartbeat({})
        await runner.stop()
        await runner.heartbeat({})               # a beat that was queued behind the stop
        after_stop = await state()
        await runner.start()                     # the coordinator starts again in this process
        await runner.heartbeat({})
        return after_stop, await state()

    assert asyncio.run(run()) == ("restarting", "alive")


def test_no_false_death_verdicts_after_a_database_outage_of_everyone(engine, ready_core, lost_events):
    """Staging 2026-11-15: MariaDB down for ~80 s. The first instance whose
    heartbeat came back saw every peer's heartbeat 90 s old and announced them
    all dead (cluster:instance_lost for live instances, instance_state 'dead')
    although they were only a few seconds behind with their own first beat."""
    clock = ClusterClock()
    api = make_service(engine, "pod-1", clock=clock)
    runner = make_service(engine, "docker-auto-runner", clock=clock, roles=["runner"])

    async def run():
        await api._start_once()
        await runner._start_once()
        await api._heartbeat_once()
        assert api.instance_state("docker-auto-runner")["state"] == "alive"

        clock.advance(90)                       # the database was gone for everyone …
        api._hb_failures = 8                    # … pod-1's beats failed meanwhile
        await api._heartbeat_once()             # pod-1 is back first
        assert api.instance_state("docker-auto-runner")["state"] == "unknown"
        assert api.role_state("runner")["state"] == "unknown"
        assert lost_events == []

        await runner._heartbeat_once()          # the runner's next beat a few seconds later
        clock.advance(5)
        await api._heartbeat_once()
        assert api.instance_state("docker-auto-runner")["state"] == "alive"
        assert lost_events == []

        clock.advance(TTL + 11)                 # a real death after the outage is still announced
        await api._heartbeat_once()
        assert api.instance_state("docker-auto-runner")["state"] == "dead"
        assert [e["instance_id"] for e in lost_events] == ["docker-auto-runner"]
        await api.stop()

    asyncio.run(run())
