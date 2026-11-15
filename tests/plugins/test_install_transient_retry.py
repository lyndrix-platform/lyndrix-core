"""B-1 — a transient pip/network failure must not leave a plugin missing for
ten minutes on a fresh instance.

A failed dependency install is fatal for the attempt (BG-10), and the cluster
reconcile backed a failed install off for 600 s (doubling to 24 h). A new pod
with an empty plugin directory whose pip run hit a PyPI hiccup therefore went
ready WITHOUT the plugin (404 on its routes) and tried again ten minutes later
at the earliest.

Now: failures of the index/network are classified as transient and retried on
a short schedule (30/60/120/300 s) before the long back-off; while such a retry
is outstanding the plugin counts as pending for boot convergence (readiness
keeps its own cap, ``LYNDRIX_READY_PLUGIN_WAIT_SECONDS``).
"""
import asyncio
import pathlib
import sys

import httpx
import pytest

import core.services  # noqa: F401  (import order: services before api)
from config import settings
from core.components.cluster.logic.cluster_service import cluster_service
from core.components.database.logic import db_service
from core.components.plugins.logic import manager as manager_mod
from core.components.plugins.logic import plugin_service as ps_mod
from core.components.plugins.logic.manager import ModuleManager
from core.components.plugins.logic.plugin_service import (
    PluginInstallError,
    is_transient_failure,
    pip_failure_is_transient,
)

sys.path.insert(0, str(pathlib.Path(__file__).parent))  # tests are not a package
from install_fakes import REPO_URL, FakePip, install_provider, plugin_archive, service_on

MID = "lyndrix.plugin.sample"

# What pip prints when the index does not answer: retry warnings first, then
# the same final lines as for a requirement that does not exist.
PIP_NETWORK_DOWN = (
    b"WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after "
    b"connection broken by 'NewConnectionError('<pip._vendor.urllib3.connection.HTTPSConnection object>: "
    b"Failed to establish a new connection: [Errno -3] Temporary failure in name resolution')': /simple/deepmerge/\n"
    + b"x" * 600 + b"\n"
    b"ERROR: Could not find a version that satisfies the requirement deepmerge==1.1.1 (from versions: none)\n"
    b"ERROR: No matching distribution found for deepmerge==1.1.1\n"
)
PIP_NO_SUCH_VERSION = (
    b"ERROR: Could not find a version that satisfies the requirement deepmerge==99.0 (from versions: 1.0, 1.1.1)\n"
    b"ERROR: No matching distribution found for deepmerge==99.0\n"
)


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("stderr", [
    PIP_NETWORK_DOWN.decode(),
    "ReadTimeoutError(\"HTTPSConnectionPool(host='pypi.org', port=443): Read timed out.\")",
    "ERROR: HTTP error 503 while getting https://files.pythonhosted.org/packages/x.whl",
    "Max retries exceeded with url: /simple/pynacl/ (Caused by SSLError(SSLEOFError(8, 'EOF occurred')))",
])
def test_index_and_network_failures_are_transient(stderr):
    assert pip_failure_is_transient(stderr) is True


@pytest.mark.parametrize("stderr", [
    PIP_NO_SUCH_VERSION.decode(),
    "ERROR: Cannot install a==1 and b==2 because these package versions have conflicting dependencies.",
    "error: subprocess-exited-with-error\n  × Building wheel for x (pyproject.toml) did not run successfully.",
    "",
])
def test_plugin_side_failures_are_not(stderr):
    assert pip_failure_is_transient(stderr) is False


def test_provider_errors_are_classified():
    request = httpx.Request("GET", "https://api.github.com/repos/x/y")
    assert is_transient_failure(httpx.ConnectError("dns", request=request))
    assert is_transient_failure(httpx.HTTPStatusError("x", request=request, response=httpx.Response(503, request=request)))
    assert not is_transient_failure(httpx.HTTPStatusError("x", request=request, response=httpx.Response(404, request=request)))
    assert not is_transient_failure(PluginInstallError("requirements.txt rejected"))
    assert not is_transient_failure(OSError(28, "No space left on device"))


def test_install_remembers_why_it_failed_and_stays_fatal(tmp_path, monkeypatch):
    pip = FakePip(returncode=1, stderr=PIP_NETWORK_DOWN)
    svc = service_on(tmp_path, monkeypatch, pip)
    install_provider(monkeypatch, plugin_archive())

    assert asyncio.run(svc.install_plugin(REPO_URL)) is False           # still fatal for the attempt
    assert list(svc.plugin_dir.iterdir()) == []                         # nothing swapped in
    assert svc.last_failure_was_transient(REPO_URL) is True

    pip.stderr = PIP_NO_SUCH_VERSION
    assert asyncio.run(svc.install_plugin(REPO_URL)) is False
    assert svc.last_failure_was_transient(REPO_URL) is False

    pip.stderr, pip.returncode_on_exit = PIP_NETWORK_DOWN, 1
    asyncio.run(svc.install_plugin(REPO_URL))
    pip.returncode_on_exit = 0
    assert asyncio.run(svc.install_plugin(REPO_URL)) is True
    assert svc.last_failure_was_transient(REPO_URL) is False            # cleared by the success


def test_pip_timeout_and_unreachable_provider_are_transient(tmp_path, monkeypatch):
    svc = service_on(tmp_path, monkeypatch, FakePip(hang=True))
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "requirements.txt").write_text("deepmerge==1.1.1\n")
    with pytest.raises(PluginInstallError) as failure:
        asyncio.run(svc._install_requirements(staged, timeout=0.05))
    assert failure.value.transient is True

    def unreachable(request):
        raise httpx.ConnectError("Temporary failure in name resolution", request=request)

    install_provider(monkeypatch, b"", handler=unreachable)
    assert asyncio.run(svc.install_plugin(REPO_URL)) is False
    assert svc.last_failure_was_transient(REPO_URL) is True


# ── the cluster reconcile retries soon ──────────────────────────────────────


class FakeInstaller:
    """``plugin_service.install_plugin`` of a fresh pod: fails ``failures``
    times for the given reason, then succeeds."""

    def __init__(self, failures: int, transient: bool = True):
        self.failures, self.transient = failures, transient
        self.calls = 0
        self.failed_transient = False

    async def install_plugin(self, url, version="latest", upgrade=False, notify=True, lead=True):
        self.calls += 1
        if self.calls <= self.failures:
            self.failed_transient = self.transient
            return False
        self.failed_transient = False
        return True

    def last_failure_was_transient(self, url):
        return self.failed_transient


@pytest.fixture()
def fresh_pod(monkeypatch):
    """A cluster instance with an empty plugin directory and one plugin in
    ``plugin_states``. Returns ``(manager, reported)``."""
    mgr = ModuleManager()
    reported = []

    async def report(module_id, version, status, message=None):
        reported.append((module_id, status))

    monkeypatch.setattr(settings, "LYNDRIX_CLUSTER_MODE", "cluster")
    monkeypatch.setattr(db_service.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
    row = {"module_id": MID, "is_active": True, "installed_version": "1.2.0",
           "desired_version": "1.2.0", "repo_url": REPO_URL}
    monkeypatch.setattr(mgr, "_load_plugin_states", lambda: [row])
    monkeypatch.setattr(mgr, "_folder_for_state", lambda r: None)
    monkeypatch.setattr(mgr, "_report_operation", report)
    monkeypatch.setattr(mgr, "_cleanup_legacy_rows", lambda *a, **k: None, raising=False)
    return mgr, reported


def _use(monkeypatch, installer):
    monkeypatch.setattr(ps_mod.plugin_service, "install_plugin", installer.install_plugin)
    monkeypatch.setattr(ps_mod.plugin_service, "last_failure_was_transient", installer.last_failure_was_transient)


async def _until(predicate, ticks: int = 400):
    for _ in range(ticks):
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


def test_a_pypi_hiccup_is_retried_within_seconds_not_minutes(fresh_pod, monkeypatch):
    mgr, reported = fresh_pod
    installer = FakeInstaller(failures=2)
    _use(monkeypatch, installer)
    monkeypatch.setattr(manager_mod, "_RECONCILE_TRANSIENT_RETRY_S", (0.02, 0.02, 0.02, 0.02))

    async def run():
        await mgr.reconcile_cluster_state(trigger="boot")
        assert installer.calls == 1
        assert MID not in mgr._reconcile_install_backoff          # NOT in the 10-minute back-off
        assert mgr._boot_pending() == [f"retry:{MID}"]            # the pod is not "converged" without it
        assert await _until(lambda: installer.calls == 3)         # nobody triggered these passes
        await mgr._install_retry_task
        assert mgr._boot_pending() == []
        assert mgr._reconcile_transient_retry == {} and mgr._reconcile_install_backoff == {}

    asyncio.run(run())
    assert reported == []                                         # never reported as failed


def test_the_short_schedule_is_30_60_120_300(fresh_pod, monkeypatch):
    mgr, _ = fresh_pod
    monkeypatch.setattr(mgr, "_schedule_install_retry", lambda: None)
    clock = {"now": 1000.0}
    import time as time_mod
    monkeypatch.setattr(time_mod, "monotonic", lambda: clock["now"])

    delays = []
    for _ in range(4):
        assert mgr._reconcile_install_failed(MID, transient=True) is True
        used, due = mgr._reconcile_transient_retry[MID]
        delays.append(due - clock["now"])
        assert mgr._reconcile_install_allowed(MID) is False
        clock["now"] = due
        assert mgr._reconcile_install_allowed(MID) is True
    assert delays == [30, 60, 120, 300]

    # Used up: the long back-off takes over and the plugin stops being "pending".
    assert mgr._reconcile_install_failed(MID, transient=True) is False
    assert mgr._boot_pending() == []
    failures, next_attempt = mgr._reconcile_install_backoff[MID]
    assert failures == 1 and next_attempt - clock["now"] == 600
    # ...and it escalates; the short schedule does not start over within the streak.
    clock["now"] = next_attempt
    assert mgr._reconcile_install_failed(MID, transient=True) is False
    assert mgr._reconcile_install_backoff[MID][1] - clock["now"] == 1200

    mgr._reconcile_install_succeeded(MID)
    assert mgr._reconcile_install_failed(MID, transient=True) is True      # a new streak


def test_a_permanent_failure_goes_straight_to_the_long_backoff(fresh_pod, monkeypatch):
    mgr, reported = fresh_pod
    installer = FakeInstaller(failures=99, transient=False)
    _use(monkeypatch, installer)

    async def run():
        await mgr.reconcile_cluster_state(trigger="boot")
        await mgr.reconcile_cluster_state(trigger="periodic")

    asyncio.run(run())
    assert installer.calls == 1 and mgr._install_retry_task is None
    assert mgr._reconcile_install_backoff[MID][0] == 1
    assert mgr._boot_pending() == []                              # a dead repo never holds readiness
    assert reported == [(MID, "failed")]


def test_exhausted_retries_report_the_failure_once(fresh_pod, monkeypatch):
    mgr, reported = fresh_pod
    installer = FakeInstaller(failures=99)
    _use(monkeypatch, installer)
    monkeypatch.setattr(manager_mod, "_RECONCILE_TRANSIENT_RETRY_S", (0.01, 0.01))

    async def run():
        await mgr.reconcile_cluster_state(trigger="boot")
        await asyncio.wait_for(mgr._install_retry_task, timeout=5)

    asyncio.run(run())
    assert installer.calls == 3                                   # first attempt + two quick retries
    assert reported == [(MID, "failed")]
    assert mgr._reconcile_install_backoff[MID][0] == 1 and mgr._boot_pending() == []


def test_retry_loop_ends_when_the_plugin_arrived_another_way(fresh_pod, monkeypatch):
    """E.g. a shared plugin directory: a peer's install put the folder there."""
    mgr, _ = fresh_pod
    installer = FakeInstaller(failures=99)
    _use(monkeypatch, installer)
    monkeypatch.setattr(manager_mod, "_RECONCILE_TRANSIENT_RETRY_S", (0.01, 0.01, 0.01, 0.01))

    async def run():
        await mgr.reconcile_cluster_state(trigger="boot")
        monkeypatch.setattr(mgr, "_load_plugin_states", lambda: [])       # row gone / nothing to install
        await asyncio.wait_for(mgr._install_retry_task, timeout=5)        # no busy loop, no leak

    asyncio.run(run())
    assert installer.calls == 1 and mgr._reconcile_transient_retry == {}


def test_database_away_keeps_the_retry_without_spinning(fresh_pod, monkeypatch):
    mgr, _ = fresh_pod
    installer = FakeInstaller(failures=1)
    _use(monkeypatch, installer)
    monkeypatch.setattr(manager_mod, "_RECONCILE_TRANSIENT_RETRY_S", (0.02, 0.02, 0.02, 0.02))

    async def run():
        await mgr.reconcile_cluster_state(trigger="boot")
        monkeypatch.setattr(db_service.db_instance, "is_connected", False, raising=False)
        await asyncio.sleep(0.1)
        assert installer.calls == 1 and mgr._boot_pending() == [f"retry:{MID}"]
        monkeypatch.setattr(db_service.db_instance, "is_connected", True, raising=False)
        assert await _until(lambda: installer.calls == 2)
        await asyncio.wait_for(mgr._install_retry_task, timeout=5)

    asyncio.run(run())
    assert mgr._reconcile_transient_retry == {}


# ── readiness: pending, but only within the cap ─────────────────────────────


def test_readiness_waits_for_the_retry_but_never_beyond_the_cap(fresh_pod, monkeypatch):
    from core.components.boot.logic.boot_service import BootPhase
    from core.services import boot_service, db_instance, vault_instance

    mgr, _ = fresh_pod
    monkeypatch.setattr(mgr, "_schedule_install_retry", lambda: None)
    monkeypatch.setattr(manager_mod, "module_manager", mgr)
    monkeypatch.setattr(boot_service, "phase", BootPhase.READY)
    monkeypatch.setattr(boot_service, "is_booting", False)
    monkeypatch.setattr(db_instance, "is_connected", True)
    monkeypatch.setattr(vault_instance, "is_connected", True)
    for name, value in (("mode", "cluster"), ("started", True), ("draining", False), ("id_conflict", None),
                        ("preflight_results", []), ("plugin_wait_s", 300.0)):
        monkeypatch.setattr(cluster_service, name, value)
    monkeypatch.setattr(type(cluster_service), "heartbeat_stale", property(lambda self: False))
    elapsed = {"s": 5.0}
    monkeypatch.setattr(cluster_service, "plugin_wait_elapsed_s", lambda: elapsed["s"])

    async def run():
        mgr._reconcile_install_failed(MID, transient=True)                 # PyPI hiccup during the boot pass
        watch = asyncio.ensure_future(mgr._watch_boot_convergence(poll_s=0.005))
        mgr._boot_reconcile_ok = True
        await asyncio.sleep(0.05)
        assert mgr.boot_converged is False
        assert cluster_service.readiness() == (False, ["plugins:converging"])   # not ready without the plugin

        elapsed["s"] = 301.0                                               # the cap is over
        assert cluster_service.readiness() == (True, [])

        elapsed["s"] = 40.0
        mgr._reconcile_install_succeeded(MID)                              # the retry went through
        await asyncio.wait_for(watch, timeout=5)
        assert mgr.boot_converged is True and cluster_service.readiness() == (True, [])

    asyncio.run(run())
