"""M3 (opus_review_core): a check that stays undecided (recheck=True — e.g.
storage "NOT shared with <instance on another host>") re-ran the whole
preflight on EVERY successful heartbeat: the heartbeat's request_preflight()
bypassed the _PREFLIGHT_MAX_QUICK_RECHECKS cap. Now heartbeat-triggered and
periodic quick re-checks share the cap, the interval is a setting, and an
unchanged finding is logged once."""
import asyncio
import logging

from cluster_harness import ClusterClock, make_engine, make_service
from core.components.cluster.logic import cluster_service as cs_mod
from core.components.cluster.logic import preflight as pf


def test_a_permanent_warning_does_not_rerun_the_preflight_on_every_heartbeat(tmp_path, monkeypatch):
    runs = []

    async def fake_checks(service):
        runs.append(1)
        return [pf.Check("storage", "warn", "not shared", recheck=True)]

    monkeypatch.setattr(pf, "run_checks", fake_checks)

    async def main():
        clock = ClusterClock()
        svc = make_service(make_engine(tmp_path), "k3s-pod-1", clock=clock, heartbeat_s=10)
        await svc.run_preflight()                    # boot run
        svc._preflight_task = asyncio.create_task(svc._preflight_loop())
        await asyncio.sleep(0.05)
        for _ in range(30):                          # 30 heartbeats = 5 minutes
            clock.advance(10)
            svc._record_heartbeat_success(clock.mono())
            await asyncio.sleep(0.02)
        svc._preflight_task.cancel()
        return len(runs), svc._preflight_delay()

    n, delay = asyncio.run(main())
    # boot run + at most the quick re-checks (+1 for a wake that arrived while a run was going)
    assert n <= 1 + cs_mod._PREFLIGHT_MAX_QUICK_RECHECKS + 1, f"{n} preflight runs in 30 heartbeats"
    assert delay == cs_mod._PREFLIGHT_REFRESH_S


def test_a_decided_run_resets_the_quick_recheck_budget(tmp_path, monkeypatch):
    answers = [True] * 8 + [False, True]

    async def fake_checks(service):
        undecided = answers.pop(0) if answers else False
        return [pf.Check("database", "warn" if undecided else "ok", "x", recheck=undecided)]

    monkeypatch.setattr(pf, "run_checks", fake_checks)

    async def main():
        svc = make_service(make_engine(tmp_path), "pod-1", clock=ClusterClock())
        for _ in range(7):
            await svc.run_preflight()
            svc._record_heartbeat_success(0.0)
        exhausted = svc._preflight_quick_rechecks
        await svc.run_preflight()                    # undecided again
        await svc.run_preflight()                    # decided
        return exhausted, svc._preflight_quick_rechecks, svc._preflight_recheck

    exhausted, after, recheck = asyncio.run(main())
    assert exhausted == cs_mod._PREFLIGHT_MAX_QUICK_RECHECKS
    assert (after, recheck) == (0, False)


def test_the_refresh_interval_is_a_setting_with_a_floor(tmp_path, monkeypatch):
    from config import settings

    svc = make_service(make_engine(tmp_path), "pod-1", clock=ClusterClock(), heartbeat_s=10)
    monkeypatch.setattr(settings, "LYNDRIX_PREFLIGHT_INTERVAL_SECONDS", 900, raising=False)
    assert svc._preflight_delay() == 900
    svc._preflight_recheck = True
    assert svc._preflight_delay() == cs_mod._PREFLIGHT_RECHECK_BEATS * 10
    monkeypatch.setattr(settings, "LYNDRIX_PREFLIGHT_INTERVAL_SECONDS", 5, raising=False)
    assert svc._preflight_delay() == cs_mod._PREFLIGHT_RECHECK_BEATS * 10   # quick re-check: never above the interval floor
    svc._preflight_recheck = False
    assert svc._preflight_delay() == cs_mod._PREFLIGHT_MIN_REFRESH_S


def test_an_unchanged_finding_is_logged_once(tmp_path, monkeypatch, caplog):
    messages = iter(["not shared with docker-auto", "not shared with docker-auto", "not shared with pod-9"])

    async def fake_checks(service):
        return [pf.Check("storage", "warn", next(messages))]

    monkeypatch.setattr(pf, "run_checks", fake_checks)
    svc = make_service(make_engine(tmp_path), "pod-1", clock=ClusterClock())
    logger = logging.getLogger(cs_mod.log.name)
    monkeypatch.setattr(logger, "propagate", True)

    async def main():
        for _ in range(3):
            await svc.run_preflight()

    with caplog.at_level(logging.DEBUG, logger=cs_mod.log.name):
        asyncio.run(main())
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "PREFLIGHT [storage]" in r.getMessage()]
    assert warnings == ["PREFLIGHT [storage] WARN: not shared with docker-auto",
                        "PREFLIGHT [storage] WARN: not shared with pod-9"]
