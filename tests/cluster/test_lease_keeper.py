"""LeaseKeeper transitions against a scripted coordinator."""
import asyncio
import time

from core.components.cluster.logic.leases import LeaseKeeper


class ScriptedCoordinator:
    driver = "fake"
    instance_id = "me"

    def __init__(self):
        self.answers: dict = {}      # name → True | False | Exception
        self.released: list = []

    async def try_acquire(self, name, ttl=None):
        answer = self.answers.get(name, True)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def release(self, name):
        self.released.append(name)
        return True


def _keeper(coord=None):
    coord = coord or ScriptedCoordinator()
    return coord, LeaseKeeper(coord, ttl=30, heartbeat_s=10)


def test_acquired_fires_once_and_lost_on_takeover():
    coord, keeper = _keeper()
    events = []
    keeper.want("x", acquired=lambda: events.append("acq"), lost=lambda: events.append("lost"))

    async def run():
        await keeper.tick()
        await keeper.tick()  # renewal → no second "acq"
        assert events == ["acq"] and keeper.holds("x") and keeper.held == ["x"]
        coord.answers["x"] = False  # a peer owns it now
        await keeper.tick()
        assert events == ["acq", "lost"] and not keeper.holds("x")
        coord.answers["x"] = True
        await keeper.tick()
        assert events == ["acq", "lost", "acq"]

    asyncio.run(run())


def test_unreachable_coordinator_fences_after_ttl():
    coord, keeper = _keeper()
    events = []
    keeper.want("x", acquired=lambda: events.append("acq"), lost=lambda: events.append("lost"))

    async def run():
        await keeper.tick()
        coord.answers["x"] = RuntimeError("db down")
        await keeper.tick()
        assert events == ["acq"] and keeper.holds("x")  # still ours within the TTL
        keeper._held["x"] = time.monotonic() - 31  # pretend the last renewal was > TTL ago
        await keeper.tick()
        assert events == ["acq", "lost"] and not keeper.holds("x")

    asyncio.run(run())


def test_late_registration_on_a_held_lease_fires_immediately():
    coord, keeper = _keeper()
    first, second = [], []
    keeper.want("x", acquired=lambda: first.append(1))
    asyncio.run(keeper.tick())
    keeper.want("x", acquired=lambda: second.append(1))
    assert first == [1] and second == [1]
    assert keeper.wanted == ["x"]


def test_forget_releases_without_firing_lost_and_release_now_fires_lost():
    coord, keeper = _keeper()
    events = []
    acq, lost = (lambda: events.append("acq")), (lambda: events.append("lost"))
    keeper.want("x", acquired=acq, lost=lost)
    keeper.want("y", acquired=acq, lost=lost)

    async def run():
        await keeper.tick()
        assert keeper.held == ["x", "y"]
        keeper.forget("x", acquired=acq, lost=lost)
        await asyncio.sleep(0.05)  # quiet release task
        assert not keeper.holds("x") and "x" in coord.released and keeper.wanted == ["y"]
        assert events == ["acq", "acq"]  # no "lost" for a forgotten lease
        assert await keeper.release_now("y") is True
        assert events == ["acq", "acq", "lost"] and "y" in coord.released
        assert await keeper.release_now("y") is False
        assert "y" in keeper.wanted  # still wanted → re-acquired on the next tick
        await keeper.tick()
        assert keeper.holds("y")

    asyncio.run(run())


def test_async_callbacks_and_on_change_hook():
    coord, keeper = _keeper()
    seen, changes = [], []

    async def acquired():
        seen.append("async-acq")

    keeper.on_change = lambda name, held: changes.append((name, held))
    keeper.want("x", acquired=acquired)

    async def run():
        await keeper.tick()
        await asyncio.sleep(0.02)
        coord.answers["x"] = False
        await keeper.tick()

    asyncio.run(run())
    assert seen == ["async-acq"] and changes == [("x", True), ("x", False)]


def test_stop_releases_everything():
    coord, keeper = _keeper()
    events = []
    keeper.want("x", lost=lambda: events.append("lost"))

    async def run():
        await keeper.start()
        await asyncio.sleep(0.05)  # first loop pass acquires
        assert keeper.holds("x")
        await keeper.stop()
        assert not keeper.holds("x") and coord.released == ["x"] and events == ["lost"]

    asyncio.run(run())
