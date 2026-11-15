"""NotificationRouter discovery sync (audit finding BG-03).

The old sync ran on every ``ui:needs_refresh`` on every instance, rewrote every
row each time, ran unlocked when the advisory lock timed out and deleted the
operator's bindings of any plugin that was momentarily not loaded — real
production lock storms (1205/1020). These tests pin the fixed behaviour.

SQLite StaticPool + a statement counter; no MariaDB needed.
"""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import tx as tx_mod
from core.components.notification_router import entrypoint as router_entrypoint
from core.components.notification_router.logic import router_service as mod
from core.components.notification_router.logic.endpoint_registry import endpoint_registry
from core.components.notification_router.logic.precedence import _db_cache, db_cache_get
from core.components.plugins.logic.manager import module_manager
from core.components.plugins.logic.models import (
    NotificationEndpoint, PluginNotificationEndpoint, PluginState,
)

PID = "lyndrix.plugin.routertest"


class Statements:
    """Counts the SQL statements issued per verb."""

    def __init__(self, engine):
        self.seen: list[str] = []
        event.listen(engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, _conn, _cursor, statement, *_rest):
        self.seen.append(statement.strip().split()[0].upper())

    def writes(self) -> list[str]:
        return [verb for verb in self.seen if verb in ("INSERT", "UPDATE", "DELETE")]

    def reset(self) -> None:
        self.seen.clear()


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    PluginNotificationEndpoint.__table__.create(bind=engine)
    PluginState.__table__.create(bind=engine)
    monkeypatch.setattr(mod.db_instance, "engine", engine, raising=False)
    monkeypatch.setattr(mod.db_instance, "SessionLocal", sessionmaker(bind=engine), raising=False)
    monkeypatch.setattr(mod.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(tx_mod.time, "sleep", lambda _s: None)
    saved_cache = dict(_db_cache)
    yield engine
    _db_cache.clear()
    _db_cache.update(saved_cache)
    endpoint_registry.remove_plugin(PID)


@pytest.fixture()
def registry(monkeypatch):
    """Replace the module registry; returns a helper to (un)load the test plugin."""
    entries: dict = {}
    monkeypatch.setattr(module_manager, "registry", entries)

    def load(*endpoint_names, **endpoint_kwargs):
        manifest = SimpleNamespace(
            id=PID,
            notification_endpoints=[NotificationEndpoint(name=n, **endpoint_kwargs) for n in endpoint_names],
        )
        entries[PID] = {"manifest": manifest}

    def unload():
        entries.pop(PID, None)

    return SimpleNamespace(load=load, unload=unload)


def _service() -> mod.NotificationRouterService:
    svc = mod.NotificationRouterService()
    svc._schema_ready = True
    return svc


def _rows(engine) -> dict:
    with sessionmaker(bind=engine)() as s:
        return {r.endpoint_name: r for r in s.query(PluginNotificationEndpoint).filter_by(plugin_id=PID)}


def _mark_installed(engine) -> None:
    with sessionmaker(bind=engine)() as s:
        s.add(PluginState(module_id=PID, is_active=True))
        s.commit()


def test_unchanged_manifests_write_nothing(db, registry):
    registry.load("alerts", "reports")
    svc = _service()
    statements = Statements(db)

    asyncio.run(svc.sync_declared_endpoints())
    assert statements.writes() == ["INSERT"] or statements.writes() == ["INSERT", "INSERT"]
    assert set(_rows(db)) == {"alerts", "reports"}

    statements.reset()
    asyncio.run(svc.sync_declared_endpoints())
    assert statements.writes() == []          # no UPDATE → no row locks for the common no-op
    assert "COMMIT" not in statements.seen


def test_only_changed_declaration_is_updated(db, registry):
    registry.load("alerts", "reports")
    svc = _service()
    asyncio.run(svc.sync_declared_endpoints())
    before = _rows(db)["reports"].updated_at

    registry.load("alerts", "reports", internal_toast=False)   # manifest changed for both
    registry.load("alerts", "reports")                          # … and back: nothing changed
    statements = Statements(db)
    asyncio.run(svc.sync_declared_endpoints())
    assert statements.writes() == []
    assert _rows(db)["reports"].updated_at == before

    entries = module_manager.registry[PID]["manifest"].notification_endpoints
    entries[0] = NotificationEndpoint(name="alerts", description="now with a description")
    asyncio.run(svc.sync_declared_endpoints())
    assert statements.writes() == ["UPDATE"]                    # exactly the one changed row


def test_operator_config_survives_a_registry_without_the_plugin(db, registry):
    registry.load("alerts", "reports")
    svc = _service()
    _mark_installed(db)

    async def run():
        await svc.sync_declared_endpoints()
        await svc.update_binding(PID, "alerts", active=False, providers=["discord"])
        registry.unload()                       # reload window / failed load on this instance
        await svc.sync_declared_endpoints()

    asyncio.run(run())
    rows = _rows(db)
    assert set(rows) == {"alerts", "reports"}   # installed elsewhere → nothing is deleted
    assert rows["alerts"].is_active is False and rows["alerts"].provider_bindings == ["discord"]
    assert db_cache_get(PID, "alerts")["provider_bindings"] == ["discord"]


def test_removed_endpoint_keeps_operator_config_but_drops_plain_rows(db, registry):
    registry.load("alerts", "reports", "legacy")
    svc = _service()
    _mark_installed(db)

    async def run():
        await svc.sync_declared_endpoints()
        await svc.update_binding(PID, "reports", providers=["discord"])
        registry.load("alerts")                 # new version dropped "reports" and "legacy"
        await svc.sync_declared_endpoints()

    asyncio.run(run())
    assert set(_rows(db)) == {"alerts", "reports"}   # "legacy" had no operator config


def test_rows_of_an_uninstalled_plugin_are_pruned(db, registry):
    registry.load("alerts")
    svc = _service()

    async def run():
        await svc.sync_declared_endpoints()
        await svc.update_binding(PID, "alerts", active=False)
        registry.unload()                       # no plugin_states row either → really uninstalled
        await svc.sync_declared_endpoints()

    asyncio.run(run())
    assert _rows(db) == {}
    assert db_cache_get(PID, "alerts") is None


def test_no_pruning_when_install_state_is_unknown(db, registry):
    registry.load("alerts")
    svc = _service()
    asyncio.run(svc.sync_declared_endpoints())
    PluginState.__table__.drop(bind=db)         # e.g. sync before the plugin tables exist
    registry.unload()
    asyncio.run(svc.sync_declared_endpoints())
    assert set(_rows(db)) == {"alerts"}


def test_concurrent_requests_coalesce(db, registry):
    registry.load("alerts")
    svc = _service()

    async def run():
        await asyncio.gather(*(svc.sync_declared_endpoints() for _ in range(5)))

    asyncio.run(run())
    assert 1 <= svc.sync_runs <= 2


def test_event_requests_are_debounced_into_one_sync(db, registry, monkeypatch):
    registry.load("alerts")
    svc = _service()
    monkeypatch.setattr(mod, "_SYNC_DEBOUNCE_SECONDS", 0.05)

    async def run():
        for _ in range(8):                      # install burst: files_changed, state_changed, …
            svc.request_sync()
            await asyncio.sleep(0.005)
        await svc._sync_task

    asyncio.run(run())
    assert svc.sync_runs == 1
    assert set(_rows(db)) == {"alerts"}


def test_request_during_a_running_sync_gets_a_follow_up_pass(db, registry):
    registry.load("alerts")
    svc = _service()
    real_sync = svc._sync

    async def run():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def slow_sync(declared, loaded):
            loop.call_soon_threadsafe(started.set)
            import time
            time.sleep(0.1)
            return real_sync(declared, loaded)

        svc._sync = slow_sync
        first = asyncio.ensure_future(svc.sync_declared_endpoints())
        await started.wait()
        registry.load("alerts", "late")         # changes while pass 1 is in its transaction
        await svc.sync_declared_endpoints()
        await first

    asyncio.run(run())
    assert svc.sync_runs == 2
    assert set(_rows(db)) == {"alerts", "late"}


def test_busy_lock_on_mysql_skips_instead_of_running_unlocked(db, registry, monkeypatch):
    registry.load("alerts")
    svc = _service()
    lock_free = {"value": False}
    lock_calls: list[dict] = []

    @contextmanager
    def fake_lock(name, timeout=60, *, warn=True):
        lock_calls.append({"name": name, "timeout": timeout, "warn": warn})
        yield lock_free["value"]

    monkeypatch.setattr(mod.db_instance, "advisory_lock", fake_lock, raising=False)
    monkeypatch.setattr(svc, "_lock_is_mandatory", lambda: True)   # behave like MariaDB for the lock decision
    monkeypatch.setattr(mod, "_BUSY_RETRY_SECONDS", (0.01, 0.02))
    statements = Statements(db)

    async def run():
        task = asyncio.ensure_future(svc.sync_declared_endpoints())
        await asyncio.sleep(0.05)
        assert statements.seen == []            # nothing ran without the lock
        assert svc.sync_skipped_busy >= 1
        lock_free["value"] = True               # the peer finished
        await task

    asyncio.run(run())
    assert set(_rows(db)) == {"alerts"}
    assert lock_calls[0]["name"] == "lyndrix_notification_sync"
    assert lock_calls[0]["timeout"] <= 3 and lock_calls[0]["warn"] is False


def test_busy_lock_gives_up_after_bounded_rounds(db, registry, monkeypatch):
    registry.load("alerts")
    svc = _service()

    @contextmanager
    def never(name, timeout=60, *, warn=True):
        yield False

    monkeypatch.setattr(mod.db_instance, "advisory_lock", never, raising=False)
    monkeypatch.setattr(svc, "_lock_is_mandatory", lambda: True)
    monkeypatch.setattr(mod, "_BUSY_RETRY_SECONDS", (0.001, 0.002))

    asyncio.run(svc.sync_declared_endpoints())
    assert svc.sync_skipped_busy == mod._BUSY_MAX_ROUNDS
    assert _rows(db) == {}


def test_a_sync_that_gave_up_is_retried_without_another_lifecycle_event(db, registry, monkeypatch):
    """N7: after the bounded busy rounds the sync was dropped until the next
    plugin lifecycle event — on a quiet system the endpoints of this instance
    stayed unsynced indefinitely."""
    registry.load("alerts")
    svc = _service()
    lock_free = {"value": False}

    @contextmanager
    def fake_lock(name, timeout=60, *, warn=True):
        yield lock_free["value"]

    monkeypatch.setattr(mod.db_instance, "advisory_lock", fake_lock, raising=False)
    monkeypatch.setattr(svc, "_lock_is_mandatory", lambda: True)
    monkeypatch.setattr(mod, "_BUSY_RETRY_SECONDS", (0.001, 0.002))
    monkeypatch.setattr(mod, "_BUSY_DEFERRED_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(mod, "_SYNC_DEBOUNCE_SECONDS", 0.0)

    async def run():
        await svc.sync_declared_endpoints()          # gives up: the caller is not held forever
        assert _rows(db) == {} and svc.sync_skipped_busy == mod._BUSY_MAX_ROUNDS
        lock_free["value"] = True                    # the peer finished; nothing else happens
        for _ in range(300):
            if _rows(db):
                break
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert set(_rows(db)) == {"alerts"}
    assert svc._deferred_retries == 0                # a pass that ran resets the delay


def test_the_deferred_retry_backs_off_while_the_lock_stays_busy(monkeypatch):
    svc = _service()

    async def run():
        delays = [svc._defer_sync() for _ in range(6)]
        svc._deferred_retry.cancel()
        return delays

    assert asyncio.run(run()) == [60, 120, 240, 480, 600, 600]


class _FakeDriverError(Exception):
    pass


def _transient(code: int) -> OperationalError:
    return OperationalError("UPDATE …", {}, _FakeDriverError(code, "transient"))


@pytest.mark.parametrize("code", [1020, 1205, 1213])
def test_transient_errors_are_retried_with_a_fresh_session(db, registry, monkeypatch, code):
    registry.load("alerts")
    svc = _service()
    real_once = svc._sync_once
    attempts = {"n": 0}

    def flaky(declared, loaded):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _transient(code)
        return real_once(declared, loaded)

    monkeypatch.setattr(svc, "_sync_once", flaky)
    asyncio.run(svc.sync_declared_endpoints())
    assert attempts["n"] == 3 and set(_rows(db)) == {"alerts"}


def test_update_binding_retries_transient_errors(db, registry, monkeypatch):
    registry.load("alerts")
    svc = _service()
    asyncio.run(svc.sync_declared_endpoints())
    real_session = tx_mod.write_session
    calls = {"n": 0}

    @contextmanager
    def flaky_session(lock_wait_seconds=5):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _transient(1020)
        with real_session(lock_wait_seconds) as session:
            yield session

    monkeypatch.setattr(tx_mod, "write_session", flaky_session)
    state = asyncio.run(svc.update_binding(PID, "alerts", active=False))
    assert calls["n"] == 2 and state.active is False
    assert _rows(db)["alerts"].is_active is False


def test_ensure_schema_runs_under_the_bootstrap_lock(db, monkeypatch):
    svc = mod.NotificationRouterService()
    held: list[str] = []

    @contextmanager
    def fake_lock(name, timeout=60, *, warn=True):
        held.append(name)
        yield True

    monkeypatch.setattr(mod.db_instance, "advisory_lock", fake_lock, raising=False)
    svc._ensure_schema()
    assert held == ["lyndrix_bootstrap"] and svc._schema_ready


def test_ensure_schema_treats_a_lost_ddl_race_as_done(db, monkeypatch):
    svc = mod.NotificationRouterService()

    @contextmanager
    def racing_lock(name, timeout=60, *, warn=True):
        raise OperationalError("CREATE TABLE …", {}, _FakeDriverError(1050, "Table already exists"))
        yield  # pragma: no cover

    monkeypatch.setattr(mod.db_instance, "advisory_lock", racing_lock, raising=False)
    svc._ensure_schema()
    assert svc._schema_ready


def test_triggers_are_local_lifecycle_events_only(monkeypatch):
    """``ui:needs_refresh`` (relayed, several per install) is no trigger; peers' relays are ignored."""
    subscribed: dict = {}
    requested: list[int] = []

    class Ctx:
        log = SimpleNamespace(info=lambda *a, **k: None)

        def subscribe(self, topic):
            def decorator(callback):
                subscribed[topic] = callback
                return callback
            return decorator

        def create_task(self, coro, name=None):
            coro.close()

    monkeypatch.setattr(router_entrypoint.db_instance, "is_connected", True, raising=False)
    monkeypatch.setattr(router_entrypoint.notification_router, "request_sync", lambda: requested.append(1))
    router_entrypoint.setup(Ctx())

    assert "ui:needs_refresh" not in subscribed
    assert "ui:needs_refresh" not in router_entrypoint.manifest.permissions.subscribe
    for topic in ("plugin:state_changed", "plugin:files_changed", "system:boot_complete"):
        assert topic in subscribed and topic in router_entrypoint.manifest.permissions.subscribe

    subscribed["plugin:state_changed"]({"plugin_id": PID, "action": "installed", "_origin": "core-b"})
    assert requested == []
    subscribed["plugin:state_changed"]({"plugin_id": PID, "action": "installed"})
    assert requested == [1]
