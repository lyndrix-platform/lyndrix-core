"""Fixtures for the multi-instance cluster scenarios.

The scenarios reuse the harness of ``tests/cluster`` (file SQLite shared by
several simulated instances, one fake clock for the database and for every
instance's monotonic clock). Everything process-global that readiness reads
(boot phase, database/Vault flags, plugin convergence) is patched here once.
"""
import pathlib
import sys

import pytest

# ``cluster_harness`` lives next to the unit-level cluster tests.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "cluster"))

import core.services  # noqa: E402,F401  — before core.api (circular import)
from cluster_harness import ClusterClock, make_engine  # noqa: E402
from core.bus import bus  # noqa: E402
from core.components.cluster.logic import cluster_service as cs_mod  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(tmp_path)
    yield eng
    eng.dispose()


@pytest.fixture()
def clock():
    return ClusterClock()


@pytest.fixture(autouse=True)
def plugins_converged(monkeypatch):
    from core.components.plugins.logic.manager import module_manager

    monkeypatch.setattr(module_manager, "boot_converged", True, raising=False)


@pytest.fixture()
def ready_core(monkeypatch):
    """Boot finished, database and Vault connected (process-global flags)."""
    from core.components.boot.logic.boot_service import BootPhase
    from core.services import boot_service, db_instance, vault_instance

    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)


@pytest.fixture()
def lost_by():
    """``lost_by("pod-1")`` → list of ``cluster:instance_lost`` payloads that
    pod-1 observed (all simulated instances emit on the one process-wide bus;
    the payload names the observer)."""
    subscribed: list = []

    def observe(observer: str) -> list:
        seen: list = []

        def _lost(payload):
            if payload.get("observed_by") == observer:
                seen.append(payload)

        bus.subscribe(cs_mod.INSTANCE_LOST_TOPIC)(_lost)
        subscribed.append(_lost)
        return seen

    yield observe
    for callback in subscribed:
        bus.unsubscribe(cs_mod.INSTANCE_LOST_TOPIC, callback)
