"""Vault seal watchdog + client timeout (audit finding BG-17).

A Vault that re-sealed or vanished after boot was never noticed
(``is_connected`` stayed True, nobody emitted ``vault:status_changed``, no
auto-unseal). The watchdog probes the seal state and drives the same events
the boot path uses.
"""
import asyncio

import pytest

import core.services  # noqa: F401  (import order: services before api)
from core.components.vault.logic import vault_service as mod
from core.components.vault.logic.vault_service import VaultService


class FakeSys:
    def __init__(self):
        self.sealed = False
        self.down = False
        self.mounts = {"lyndrix/": {}}

    def is_sealed(self):
        if self.down:
            raise ConnectionError("vault unreachable")
        return self.sealed

    def list_mounted_secrets_engines(self):
        return self.mounts


@pytest.fixture()
def vault(monkeypatch):
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(mod.bus, "emit", lambda topic, payload=None, **kw: emitted.append((topic, payload)))
    monkeypatch.setattr(mod.bus, "subscribe", lambda topic: (lambda cb: cb))
    svc = VaultService()
    svc.client = type("Client", (), {"sys": FakeSys(), "token": None})()
    svc._start_watchdog = lambda: None            # ticks are driven by the tests
    svc.is_connected = True
    svc.ui_state = "ready"
    svc.emitted = emitted
    monkeypatch.setattr(mod.settings, "LYNDRIX_MASTER_KEY", None, raising=False)
    return svc


def _topics(svc):
    return [t for t, _ in svc.emitted]


def test_client_has_a_short_timeout():
    assert VaultService().client.adapter._kwargs["timeout"] == 5   # hvac default: 30 s


def test_healthy_vault_emits_nothing(vault):
    asyncio.run(vault._watchdog_tick())
    assert vault.is_connected and vault.emitted == []


def test_reseal_is_detected_and_asks_for_unseal(vault):
    vault.client.sys.sealed = True
    asyncio.run(vault._watchdog_tick())

    assert vault.is_connected is False and vault.ui_state == "needs_unseal"
    assert ("vault:status_changed", {"state": "needs_unseal"}) in vault.emitted
    assert "vault:needs_unseal" in _topics(vault)          # auto-unseal listens to this


def test_sealed_vault_retries_auto_unseal_only_with_a_master_key(vault, monkeypatch):
    vault.client.sys.sealed = True
    asyncio.run(vault._watchdog_tick())
    vault.emitted.clear()

    asyncio.run(vault._watchdog_tick())                    # still sealed, no master key
    assert vault.emitted == []                             # nothing to retry, no event noise

    monkeypatch.setattr(mod.settings, "LYNDRIX_MASTER_KEY", "dummy-master-key-not-real", raising=False)
    asyncio.run(vault._watchdog_tick())
    assert _topics(vault) == ["vault:needs_unseal"]


def test_recovery_reemits_readiness_like_boot(vault):
    vault.client.sys.sealed = True
    asyncio.run(vault._watchdog_tick())
    vault.emitted.clear()

    vault.client.sys.sealed = False                        # unsealed again (operator / auto-unseal)
    asyncio.run(vault._watchdog_tick())

    assert vault.is_connected and vault.ui_state == "ready"
    assert _topics(vault) == ["vault:status_changed", "vault:opened", "vault:ready_for_data"]


def test_single_unreachable_probe_is_tolerated_then_reported(vault):
    vault.client.sys.down = True
    asyncio.run(vault._watchdog_tick())
    assert vault.is_connected and vault.emitted == []      # one blip does not flap the platform

    asyncio.run(vault._watchdog_tick())
    assert vault.is_connected is False and vault.ui_state == "error"
    assert vault.emitted == [("vault:status_changed", {"state": "error"})]

    vault.client.sys.down = False
    asyncio.run(vault._watchdog_tick())
    assert vault.is_connected and vault.ui_state == "ready"
    assert "vault:ready_for_data" in _topics(vault)


def test_recovery_without_a_working_token_stays_disconnected(vault):
    vault.client.sys.sealed = True
    asyncio.run(vault._watchdog_tick())
    vault.client.sys.sealed = False

    def denied():
        raise PermissionError("permission denied")

    vault.client.sys.list_mounted_secrets_engines = denied
    asyncio.run(vault._watchdog_tick())
    assert vault.is_connected is False
    assert "vault:opened" not in _topics(vault)


def test_watchdog_loop_survives_errors_and_starts_once(vault, monkeypatch):
    monkeypatch.setattr(mod, "_WATCHDOG_INTERVAL_SECONDS", 0.01)
    ticks = {"n": 0}

    async def failing_tick():
        ticks["n"] += 1
        raise RuntimeError("unexpected")

    vault._watchdog_tick = failing_tick

    async def run():
        task = asyncio.ensure_future(vault._watchdog())
        await asyncio.sleep(0.08)
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert ticks["n"] >= 2
