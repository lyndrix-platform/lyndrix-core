"""First database connect keeps retrying (audit finding BG-18).

The loop used to end for good after 30 attempts (~2.5 min): a core that booted
before MariaDB stayed up but unready until someone restarted it.
"""
import asyncio

import pytest

import core.services  # noqa: F401  (import order: services before api)
from core.components.database.logic import db_service as mod
from core.components.database.logic.db_service import DatabaseService


@pytest.fixture()
def service(monkeypatch):
    svc = DatabaseService()
    emitted: list[tuple[str, dict]] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(mod.bus, "emit", lambda topic, payload=None, **kw: emitted.append((topic, payload)))

    def fake_task(coro, name=None):
        coro.close()                      # the watchdog is not part of these tests

    monkeypatch.setattr(mod.bus, "create_tracked_task", fake_task)
    svc.emitted, svc.sleeps = emitted, sleeps
    return svc


def test_first_connect_survives_the_fast_phase_and_connects_late(service):
    attempts = {"n": 0}
    comes_up_at = service.max_retries + 25

    def check():
        attempts["n"] += 1
        if attempts["n"] < comes_up_at:
            raise ConnectionError("Can't connect to MySQL server on 'db' (timed out)")

    service._check_db_sync = check
    asyncio.run(service._connection_loop())

    assert attempts["n"] == comes_up_at                  # kept going past the old limit
    assert service.is_connected and service.failure_reason is None
    topics = [t for t, _ in service.emitted]
    assert topics.count("db:connected") == 1
    # The "exhausted" hint is announced exactly once, then cleared by the success.
    hints = [p for t, p in service.emitted if t == "system:maintenance_mode" and p.get("reason") == "max_retries"]
    assert len(hints) == 1
    assert ("system:maintenance_mode", {"service": "db", "active": False}) in service.emitted
    # Backoff after the fast phase is capped.
    slow = service.sleeps[service.max_retries - 1:]
    assert slow and max(slow) <= 30 and slow[-1] > 5


def test_boot_screen_hint_is_set_while_still_retrying(service, monkeypatch):
    def check():
        raise ConnectionError("connection refused")

    service._check_db_sync = check
    slept = {"n": 0}

    async def sleep_then_stop(_seconds):
        slept["n"] += 1
        if slept["n"] > service.max_retries + 3:
            raise asyncio.CancelledError()            # end the test; the loop itself never stops

    monkeypatch.setattr(mod.asyncio, "sleep", sleep_then_stop)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await service._connection_loop()

    asyncio.run(run())
    assert service.connect_attempts > service.max_retries
    assert service.failure_reason == "max_retries" and not service.is_connected


def test_config_errors_stay_terminal(service):
    def check():
        raise RuntimeError("(1045, \"Access denied for user 'lyndrix'@'10.0.0.1'\")")

    service._check_db_sync = check
    asyncio.run(service._connection_loop())
    assert service.failure_reason == "config_error" and not service.is_connected
    assert service.connect_attempts == 1
