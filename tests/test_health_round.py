"""``/api/health`` plugin round: single-flight refresh + skip contract (BG-23)."""
import asyncio
from types import SimpleNamespace

import core.services  # noqa: F401  (import order: services before api)
from core.api import PluginHealthStatus
from core.api import health_round as mod


def _manager(**plugins):
    """Fake module manager; each plugin = (health coroutine function, runtime dict)."""
    registry = {}
    for pid, (health, runtime) in plugins.items():
        registry[pid] = {
            "manifest": SimpleNamespace(type="PLUGIN"),
            "status": "active",
            "module": SimpleNamespace(health=health),
            "context": SimpleNamespace(runtime=runtime),
        }
    return SimpleNamespace(registry=registry, get_runtime=lambda pid: registry[pid]["context"].runtime)


def test_concurrent_pollers_share_one_round():
    calls = {"n": 0}

    async def health(_ctx):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return PluginHealthStatus(status="ok")

    manager = _manager(p1=(health, None), p2=(health, None))
    rounds = mod.PluginHealthRound()

    async def run():
        return await asyncio.gather(*(rounds.get(manager) for _ in range(10)))

    results = asyncio.run(run())
    assert calls["n"] == 2                                  # one round (2 plugins), not 10
    assert [cached for _w, _p, cached in results].count(False) == 1
    assert all(worst == "ok" and set(plugins) == {"p1", "p2"} for worst, plugins, _c in results)


def test_cache_expiry_triggers_exactly_one_new_round(monkeypatch):
    calls = {"n": 0}

    async def health(_ctx):
        calls["n"] += 1
        return PluginHealthStatus(status="degraded")

    manager = _manager(p1=(health, None))
    rounds = mod.PluginHealthRound()

    async def run():
        assert (await rounds.get(manager))[2] is False
        assert (await rounds.get(manager))[2] is True      # within the TTL
        monkeypatch.setattr(mod, "CACHE_TTL_SECONDS", 0.0)
        await asyncio.gather(*(rounds.get(manager) for _ in range(5)))

    asyncio.run(run())
    assert calls["n"] >= 2
    assert rounds._worst == "degraded"


def test_health_is_skipped_while_starting_or_in_blocking_maintenance():
    probed: list[str] = []

    def probe(name):
        async def health(_ctx):
            probed.append(name)
            return PluginHealthStatus(status="error")
        return health

    manager = _manager(
        starting=(probe("starting"), {"phase": "starting", "blocking": True}),
        warmup_banner=(probe("warmup_banner"), {"phase": "starting", "blocking": False}),
        maintenance=(probe("maintenance"), {"phase": "maintenance", "blocking": True}),
        banner=(probe("banner"), {"phase": "maintenance", "blocking": False}),
        ready=(probe("ready"), {"phase": "ready", "blocking": False}),
    )
    worst, plugins, _cached = asyncio.run(mod.PluginHealthRound().get(manager))

    assert sorted(probed) == ["banner", "ready"]            # non-blocking maintenance is still probed
    assert plugins["maintenance"]["status"] == "unknown"
    assert plugins["maintenance"]["details"] == {"reason": "maintenance"}
    assert plugins["starting"]["details"] == {"reason": "starting"}
    assert worst == "error"


def test_failing_and_slow_health_are_reported_not_raised(monkeypatch):
    async def boom(_ctx):
        raise RuntimeError("db gone")

    async def slow(_ctx):
        await asyncio.sleep(1)

    monkeypatch.setattr(mod, "HEALTH_TIMEOUT_SECONDS", 0.02)
    worst, plugins, _ = asyncio.run(mod.PluginHealthRound().get(_manager(a=(boom, None), b=(slow, None))))
    assert worst == "error"
    assert plugins["a"]["details"]["reason"] == "db gone"
    assert "timed out" in plugins["b"]["details"]["reason"]


def test_hanging_health_is_not_called_again_while_it_still_runs(monkeypatch):
    """A health() stuck in a worker thread must not get a new thread per poll window."""
    import threading

    release = threading.Event()
    started = {"n": 0}

    def blocked():
        started["n"] += 1
        release.wait(5)
        return PluginHealthStatus(status="ok")

    async def hanging(_ctx):
        return await asyncio.to_thread(blocked)

    async def fine(_ctx):
        return PluginHealthStatus(status="ok")

    monkeypatch.setattr(mod, "HEALTH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(mod, "CACHE_TTL_SECONDS", 0.0)       # every get() is a new round
    manager = _manager(stuck=(hanging, None), ok=(fine, None))
    rounds = mod.PluginHealthRound()

    async def run():
        _w, first, _ = await rounds.get(manager)
        assert first["stuck"]["status"] == "error" and "timed out" in first["stuck"]["details"]["reason"]
        for _ in range(3):                                    # later poll windows
            worst, later, _ = await rounds.get(manager)
            assert later["stuck"]["status"] == "degraded"
            assert later["stuck"]["details"] == {"reason": "health check still running"}
            assert later["ok"]["status"] == "ok" and worst == "degraded"
        assert started["n"] == 1                              # ONE thread, not one per round

        release.set()                                         # the mount came back
        await asyncio.sleep(0.05)
        _w, healed, _ = await rounds.get(manager)
        assert healed["stuck"]["status"] == "ok"
        assert started["n"] == 2

    asyncio.run(run())
