"""Small helpers shared by the scenario tests (imported as ``scenario_kit``)."""
import asyncio
import time


class Recorder:
    """Records lease transitions of several simulated instances in one ordered
    log: ``(instance_id, "acquired" | "lost", lease_name)``."""

    def __init__(self, clock=None):
        self._clock = clock                     # ClusterClock: stamps every event with the monotonic time
        self.at: dict[tuple, float] = {}        # (instance, event, lease) -> time of the LAST such event
        self.log: list[tuple] = []
        self.tokens: dict[tuple, int] = {}      # (instance, lease) -> token of the last acquisition

    def watch(self, svc, name: str) -> None:
        who = svc.instance_id

        def acquired(token):
            self._note(who, "acquired", name)
            self.tokens[(who, name)] = token

        def lost():
            self._note(who, "lost", name)

        svc.leases.want(name, acquired=acquired, lost=lost)

    def _note(self, who: str, event: str, name: str) -> None:
        self.log.append((who, event, name))
        if self._clock is not None:
            self.at[(who, event, name)] = self._clock.mono.now

    def events(self, name: str) -> list[tuple]:
        return [(who, event) for who, event, lease in self.log if lease == name]

    def index(self, who: str, event: str, name: str, nth: int = 0) -> int:
        hits = [i for i, entry in enumerate(self.log) if entry == (who, event, name)]
        return hits[nth]


async def eventually(predicate, timeout: float = 3.0, step: float = 0.02) -> bool:
    """Poll a condition on real time (event delivery runs on real poll loops)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


async def stop_all(*services) -> None:
    for svc in services:
        try:
            await svc.stop()
        except Exception:
            pass
