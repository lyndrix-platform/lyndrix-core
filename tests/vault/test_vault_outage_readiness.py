"""M4 — a Vault outage after the boot must not take every instance out of rotation.

The seal watchdog (BG-17) sets ``is_connected = False`` when Vault is gone, and
``/api/ready`` required Vault. With ONE Vault behind N cores, a restart of the
Vault pod (~60 s) turned every core instance 503 at the same moment: no login,
no SSE, no UI — although none of that needs Vault.

Default now: after the boot, a lost Vault is reported (``degraded``) but the
instance stays ready. ``LYNDRIX_READY_REQUIRES_VAULT=true`` restores the strict
behaviour. During boot (Vault never opened) readiness stays 503.
"""
import asyncio

import pytest

import core.services  # noqa: F401  (import order: services before api)
from config import settings
from core.components.boot.logic.boot_service import BootPhase
from core.components.cluster.logic.cluster_service import cluster_service
from core.components.cluster.logic.preflight import run_checks
from core.components.vault.logic import kv_helper, vault_service as mod
from core.components.vault.logic.vault_service import VaultService
from core.services import boot_service, db_instance, vault_instance


@pytest.fixture()
def booted(monkeypatch):
    """A single-mode instance past its boot, everything healthy."""
    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "ui_state", "ready")
    monkeypatch.setattr(cluster_service, "mode", "single")
    monkeypatch.setattr(cluster_service, "draining", False)
    monkeypatch.setattr(cluster_service, "preflight_results", [])
    monkeypatch.setattr(settings, "LYNDRIX_READY_REQUIRES_VAULT", False, raising=False)


def _lose_vault(monkeypatch):
    monkeypatch.setattr(vault_instance, "is_connected", False)
    monkeypatch.setattr(vault_instance, "ui_state", "error")


# ── readiness ───────────────────────────────────────────────────────────────


def test_vault_outage_after_boot_keeps_the_instance_ready(booted, monkeypatch):
    assert settings.model_fields["LYNDRIX_READY_REQUIRES_VAULT"].default is False
    assert cluster_service.readiness() == (True, []) and cluster_service.degraded() == []

    _lose_vault(monkeypatch)
    assert cluster_service.readiness() == (True, [])        # was (False, ["vault"])
    assert cluster_service.degraded() == ["vault"]

    monkeypatch.setattr(vault_instance, "is_connected", True)
    assert cluster_service.degraded() == []


def test_strict_setting_fails_readiness_as_before(booted, monkeypatch):
    monkeypatch.setattr(settings, "LYNDRIX_READY_REQUIRES_VAULT", True, raising=False)
    _lose_vault(monkeypatch)
    assert cluster_service.readiness() == (False, ["vault"])
    assert cluster_service.degraded() == []                 # it is a readiness reason, not "degraded"


def test_during_boot_vault_still_gates_readiness(booted, monkeypatch):
    """Vault never opened: the instance cannot serve anything yet."""
    monkeypatch.setattr(boot_service, "phase", BootPhase.WAITING_CORE)
    monkeypatch.setattr(boot_service, "is_booting", True)
    monkeypatch.setattr(db_instance, "is_connected", False)
    _lose_vault(monkeypatch)
    ok, reasons = cluster_service.readiness()
    assert ok is False and "vault" in reasons and any(r.startswith("boot:") for r in reasons)
    assert cluster_service.degraded() == []


def test_other_reasons_are_not_softened(booted, monkeypatch):
    _lose_vault(monkeypatch)
    monkeypatch.setattr(db_instance, "is_connected", False)
    assert cluster_service.readiness() == (False, ["database"])


# ── the outage is visible ───────────────────────────────────────────────────


def test_probes_report_the_outage_without_failing(booted, monkeypatch):
    import main
    from core.api.health_round import plugin_health_round
    from fastapi.testclient import TestClient

    async def no_plugins(_manager):
        return "ok", {}, False

    monkeypatch.setattr(plugin_health_round, "get", no_plugins)
    client = TestClient(main.app, raise_server_exceptions=False)

    ready = client.get("/api/ready")
    assert ready.status_code == 200 and ready.json()["degraded"] == []
    assert client.get("/api/health").json() == {"status": "ok", "ready": True}

    _lose_vault(monkeypatch)
    ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True and ready.json()["degraded"] == ["vault"]
    assert client.get("/api/health").json() == {"status": "degraded", "ready": True}

    monkeypatch.setattr(settings, "LYNDRIX_READY_REQUIRES_VAULT", True, raising=False)
    ready = client.get("/api/ready")
    assert ready.status_code == 503 and ready.json()["reasons"] == ["vault"]


def test_preflight_names_the_outage(booted, monkeypatch):
    check = {c.id: c for c in asyncio.run(run_checks(cluster_service))}["vault"]
    assert check.status == "ok"

    _lose_vault(monkeypatch)
    check = {c.id: c for c in asyncio.run(run_checks(cluster_service))}["vault"]
    assert check.status == "warn" and check.recheck and "stays in rotation" in check.hint

    monkeypatch.setattr(settings, "LYNDRIX_READY_REQUIRES_VAULT", True, raising=False)
    check = {c.id: c for c in asyncio.run(run_checks(cluster_service))}["vault"]
    assert check.status == "warn" and "503" in check.hint


# ── secrets fail cleanly while Vault is away ────────────────────────────────


def test_secret_access_fails_cleanly_and_writes_nothing(booted, monkeypatch):
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError("Vault must not be called while it is known to be away")

    _lose_vault(monkeypatch)
    monkeypatch.setattr(vault_instance, "client", Untouchable())

    with pytest.raises(kv_helper.VaultReadError):
        kv_helper.read_secret_strict("core/settings")
    assert kv_helper.read_secret_dict("core/settings") == {}
    assert kv_helper.write_secret_dict("core/settings", {"k": "v"}) is False
    # A read-modify-write aborts instead of writing over data it never saw.
    with pytest.raises(kv_helper.VaultReadError):
        kv_helper.update_secret_dict("core/settings", lambda current: current.update(k="v"))


# ── the watchdog brings Vault back by itself ────────────────────────────────


class FakeSys:
    def __init__(self):
        self.down = False

    def is_sealed(self):
        if self.down:
            raise ConnectionError("vault unreachable")
        return False

    def list_mounted_secrets_engines(self):
        return {"lyndrix/": {}}


@pytest.fixture()
def vault(monkeypatch):
    monkeypatch.setattr(mod.bus, "emit", lambda topic, payload=None, **kw: None)
    monkeypatch.setattr(mod.bus, "subscribe", lambda topic: (lambda cb: cb))
    svc = VaultService()
    svc.client = type("Client", (), {"sys": FakeSys(), "token": None})()
    svc._start_watchdog = lambda: None
    svc.is_connected = True
    svc.ui_state = "ready"
    monkeypatch.setattr(mod.settings, "LYNDRIX_MASTER_KEY", None, raising=False)
    return svc


def test_watchdog_probes_faster_while_vault_is_lost(vault):
    assert vault._watchdog_delay() == mod._WATCHDOG_INTERVAL_SECONDS
    vault.client.sys.down = True
    asyncio.run(vault._watchdog_tick())                    # first failed probe: confirm it soon
    assert vault.is_connected and vault._watchdog_delay() == mod._WATCHDOG_RECOVERY_INTERVAL_SECONDS
    asyncio.run(vault._watchdog_tick())
    assert not vault.is_connected and vault._watchdog_delay() == mod._WATCHDOG_RECOVERY_INTERVAL_SECONDS
    vault.client.sys.down = False
    asyncio.run(vault._watchdog_tick())
    assert vault.is_connected and vault._watchdog_delay() == mod._WATCHDOG_INTERVAL_SECONDS


def test_watchdog_loop_recovers_without_any_outside_event(vault, monkeypatch):
    """Restart of the Vault pod: gone for a while, then back and unsealed."""
    monkeypatch.setattr(mod, "_WATCHDOG_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(mod, "_WATCHDOG_RECOVERY_INTERVAL_SECONDS", 0.01)

    async def wait_for(predicate):
        for _ in range(300):
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    async def run():
        task = asyncio.ensure_future(vault._watchdog())
        try:
            vault.client.sys.down = True
            assert await wait_for(lambda: not vault.is_connected)
            assert vault.ui_state == "error"
            vault.client.sys.down = False
            assert await wait_for(lambda: vault.is_connected)
            assert vault.ui_state == "ready"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
