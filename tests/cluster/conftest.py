"""Shared fixtures for the cluster tests."""
import pytest


@pytest.fixture(autouse=True)
def plugins_converged(monkeypatch):
    """The module manager singleton starts with ``boot_converged = False`` and
    only a real boot sets it. Cluster tests that are not about the
    ``plugins:converging`` readiness reason run with a converged manager;
    the ones that are patch the flag themselves.
    """
    import core.services  # noqa: F401  (import order: services before the manager)
    from core.components.plugins.logic.manager import module_manager

    monkeypatch.setattr(module_manager, "boot_converged", True, raising=False)
