"""Lease safety (CL-04): ``lost`` fires before a peer can hold the lease, a
hung renewal neither delays the other leases nor resurrects one, and every
acquisition carries a larger fencing token.

Model used by the keeper tests: two real ``LeaseKeeper``s share one lease
table that behaves like the db driver (expiry = database time at the start of
the call + ttl; a peer may take a row once it expired). Database time and the
keepers' monotonic clocks are one ``FakeClock``, so every ordering below is
exact, not "usually".
"""
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, text, update

from cluster_harness import make_engine
from core.components.cluster.logic.drivers.db import DbCoordinator
from core.components.cluster.logic.drivers.local import LocalCoordinator
from core.components.cluster.logic.leases import LeaseKeeper
from core.components.cluster.logic.models import ClusterLease

TTL, HB, MARGIN = 10.0, 2.0, 2.0     # margin(10) = max(min 2, min(hb 2, ttl/4 2.5)) = 2


class FakeClock:
    def __init__(self, start: float = 100.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LeaseTable:
    def __init__(self):
        self.rows: dict = {}     # name → (holder, expires)


class TableCoordinator:
    """One instance's view of the shared table; ``mode`` scripts its database."""
    driver = "fake"

    def __init__(self, table: LeaseTable, me: str, clock: FakeClock):
        self.table, self.instance_id, self.clock = table, me, clock
        self.mode = "ok"                 # ok | hang | fail | slow_commit
        self.slow_s = 0.0
        self.hang_names: set = set()     # hang only for these leases (empty = all, in "hang" mode)
        self.gate = None                 # asyncio.Event a hung call waits on (None = forever)
        self.in_flight = 0
        self.max_in_flight = 0
        self.released: list = []

    async def try_acquire(self, name, ttl=None):
        ttl = ttl or TTL
        if self.mode == "fail":
            raise ConnectionError("db unreachable")
        started = self.clock()           # database clock read at the start of the transaction
        if self.mode == "hang" and (not self.hang_names or name in self.hang_names):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                if self.gate is None:
                    await asyncio.sleep(3600)
                else:
                    await self.gate.wait()
            finally:
                self.in_flight -= 1
        holder, expires = self.table.rows.get(name, (None, -1.0))
        if holder in (self.instance_id, None) or expires < started:
            self.table.rows[name] = (self.instance_id, started + ttl)
            if self.mode == "slow_commit":
                self.clock.advance(self.slow_s)     # the COMMIT acknowledgement arrives late
                self.mode = "fail"
            return True
        return False

    async def release(self, name):
        self.released.append(name)
        if self.table.rows.get(name, (None,))[0] == self.instance_id:
            self.table.rows.pop(name, None)
            return True
        return False


def _pair(clock, hb=HB):
    table = LeaseTable()
    ca, cb = TableCoordinator(table, "A", clock), TableCoordinator(table, "B", clock)
    ka = LeaseKeeper(ca, ttl=TTL, heartbeat_s=hb, clock=clock)
    kb = LeaseKeeper(cb, ttl=TTL, heartbeat_s=hb, clock=clock)
    return table, ca, cb, ka, kb


def _record(keeper, names, clock, log, who):
    for name in names:
        keeper.want(name,
                    acquired=lambda n=name: log.append((clock(), who, "acquired", n)),
                    lost=lambda n=name: log.append((clock(), who, "lost", n)))


def _when(log, who, event, name):
    return next(t for t, w, e, n in log if (w, e, n) == (who, event, name))


# --- ordering: lost before a peer can acquire ---------------------------------------

def test_hung_database_fences_every_lease_before_a_peer_can_take_it():
    """p2 scenario 1: A holds three leases and its database calls hang. The old
    keeper waited a full TTL per lease, one after the other, and fenced the
    k-th lease (k−1)·TTL after a peer already held it."""
    names = ["core:leader", "plugin:x", "plugin:y"]
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock, hb=0.05)      # short real waits; the fake clock rules
    assert ka.margin(TTL) == MARGIN
    log: list = []
    _record(ka, names, clock, log, "A")
    _record(kb, names, clock, log, "B")

    async def run():
        t0 = clock()
        await ka.tick()                                  # A acquires all three at t0
        await kb.tick()
        assert ka.held == sorted(names) and kb.held == []
        ca.mode = "hang"                                 # dead socket: every call of A hangs from now on
        for _ in range(40):                              # 40 × 0.5 s of cluster time
            clock.advance(0.5)
            ka.fence_expired()                           # what the watchdog does at the deadline
            await ka.tick()
            await kb.tick()
        return t0

    t0 = asyncio.run(run())
    for name in names:
        a_lost, b_acquired = _when(log, "A", "lost", name), _when(log, "B", "acquired", name)
        assert a_lost <= t0 + TTL - MARGIN + 0.5, f"{name}: fenced {a_lost - t0:.1f}s after the last renewal"
        assert a_lost < b_acquired, f"{name}: B held it {a_lost - b_acquired:.1f}s before A let go"
        assert b_acquired > t0 + TTL                     # B never got it before the row expired
    assert ca.max_in_flight <= LeaseKeeper.MAX_INFLIGHT * len(names)   # hung calls do not pile up


def test_slow_commit_does_not_extend_the_holding_past_the_database_expiry():
    """p2 scenario 2: the renewal's row is written at the start of the call, the
    acknowledgement arrives 0.8·TTL later, then the database is gone. The old
    keeper started its fence clock at the END of that call."""
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock, hb=0.05)
    log: list = []
    _record(ka, ["x"], clock, log, "A")
    _record(kb, ["x"], clock, log, "B")

    async def run():
        await ka.tick()
        clock.advance(3)
        ca.mode, ca.slow_s = "slow_commit", 0.8 * TTL
        await ka.tick()                                  # row renewed at t+3, answer at t+11
        renewed_at = table.rows["x"][1] - TTL
        for _ in range(40):
            clock.advance(0.5)
            ka.fence_expired()
            await ka.tick()
            await kb.tick()
        return renewed_at

    renewed_at = asyncio.run(run())
    a_lost, b_acquired = _when(log, "A", "lost", "x"), _when(log, "B", "acquired", "x")
    assert a_lost <= renewed_at + TTL, "A still acted as holder after the row had expired"
    assert a_lost < b_acquired


def test_a_late_answer_cannot_resurrect_a_fenced_lease():
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock, hb=0.05)
    log: list = []
    _record(ka, ["x"], clock, log, "A")

    async def run():
        await ka.tick()
        ca.mode, ca.gate = "hang", asyncio.Event()
        clock.advance(3)
        await ka.tick()                                  # the renewal hangs
        clock.advance(TTL)                               # … past the deadline
        assert ka.fence_expired() == ["x"]
        ca.mode = "ok"
        ca.gate.set()                                    # the abandoned call finally answers "granted"
        await asyncio.sleep(0.05)
        assert not ka.holds("x") and ka.held == []       # the late answer is ignored
        assert [e for _, _, e, _ in log] == ["acquired", "lost"]
        await ka.tick()                                  # a real, new acquisition
        assert ka.holds("x")

    asyncio.run(run())
    assert [e for _, _, e, _ in log] == ["acquired", "lost", "acquired"]


def test_one_hung_renewal_does_not_hold_up_the_other_leases():
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock, hb=0.05)
    log: list = []
    _record(ka, ["a", "b", "c"], clock, log, "A")

    async def run():
        await ka.tick()
        ca.mode, ca.hang_names = "hang", {"a"}           # only the renewal of "a" hangs
        started = time.monotonic()
        for _ in range(6):
            clock.advance(1)
            await ka.tick()
        return time.monotonic() - started

    elapsed = asyncio.run(run())
    assert elapsed < 3                                    # each tick is bounded by one interval
    assert ka._held["b"].since == ka._held["c"].since == clock() and ka.holds("b") and ka.holds("c")
    assert ka._held["a"].since == clock() - 6            # not renewed, not yet fenced (6 s < 8 s)
    assert ca.max_in_flight == LeaseKeeper.MAX_INFLIGHT  # … and no thread pile-up behind it


def test_watchdog_fences_at_the_deadline_without_waiting_for_a_tick():
    """Real time: three leases, the coordinator hangs. Every ``lost`` fires at
    the common deadline — the old keeper needed one TTL per lease."""
    ttl, hb = 2.0, 0.5                                    # margin = min(max(0.1, 0.5), 1.0) = 0.5 → deadline 1.5 s
    table = LeaseTable()
    clock = time.monotonic
    coord = TableCoordinator(table, "A", clock)
    keeper = LeaseKeeper(coord, ttl=ttl, heartbeat_s=hb, min_margin_s=0.1)
    lost: dict = {}
    for name in ("a", "b", "c"):
        keeper.want(name, lost=lambda n=name: lost.setdefault(n, time.monotonic()))

    async def run():
        await keeper.start()
        await asyncio.sleep(0.15)
        assert keeper.held == ["a", "b", "c"]
        since = max(h.since for h in keeper._held.values())
        coord.mode = "hang"
        await asyncio.sleep(2.3)
        await keeper.stop()
        return since

    since = asyncio.run(run())
    assert sorted(lost) == ["a", "b", "c"]
    for name, at in lost.items():
        # expected 1.5 s after the last renewal; generous window, but always below the TTL
        assert 1.0 <= at - since < ttl, f"'{name}' fenced {at - since:.2f}s after its last renewal (ttl {ttl}s)"


# --- hand-over order -----------------------------------------------------------------

def test_release_now_runs_lost_callbacks_before_the_release():
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock, hb=0.05)
    order: list = []

    async def stop_work():
        order.append("lost:start")
        await asyncio.sleep(0.05)
        order.append("lost:done")

    ka.want("x", lost=stop_work)
    ka.want("x", lost=lambda: order.append("lost:sync"))

    async def run():
        await ka.tick()
        original = ca.release

        async def recording_release(name):
            order.append("release")
            return await original(name)

        ca.release = recording_release
        assert await ka.release_now("x") is True

    asyncio.run(run())
    # every lost callback is called first (the async one starts on the next loop
    # pass), the release waits until the async one is done
    assert order == ["lost:sync", "lost:start", "lost:done", "release"]


def test_a_hanging_lost_callback_delays_the_release_only_for_a_bounded_time(monkeypatch):
    monkeypatch.setattr(LeaseKeeper, "LOST_CALLBACK_TIMEOUT_S", 0.1)
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock, hb=0.05)

    async def never_stops():
        await asyncio.sleep(3600)

    ka.want("x", lost=never_stops)

    async def run():
        await ka.tick()
        started = time.monotonic()
        await ka.stop()
        return time.monotonic() - started

    assert asyncio.run(run()) < 2
    assert ca.released == ["x"]


def test_release_lease_fires_lost_before_the_background_release():
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock, hb=0.05)
    order: list = []

    async def stop_work():
        await asyncio.sleep(0.03)
        order.append("lost:done")

    ka.want("x", lost=stop_work)

    async def run():
        await ka.tick()
        original = ca.release

        async def recording_release(name):
            order.append("release")
            return await original(name)

        ca.release = recording_release
        ka.forget("x", lost=stop_work, fire_lost=True)   # what ctx.release_lease does
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert order == ["lost:done", "release"]


def test_a_kick_during_a_tick_is_not_lost():
    """A peer's release broadcast that arrives while a tick is under way must
    start the next pass right away, not after a whole heartbeat interval."""
    clock = time.monotonic
    table = LeaseTable()
    coord = TableCoordinator(table, "A", clock)
    table.rows["x"] = ("B", time.monotonic() + 3600)     # held by a peer for now
    calls: list = []
    original = coord.try_acquire

    async def slow_try(name, ttl=None):
        answer = await original(name, ttl)               # the database decided …
        calls.append(answer)
        await asyncio.sleep(0.15)                        # … the answer is under way while the peer releases
        return answer

    coord.try_acquire = slow_try
    keeper = LeaseKeeper(coord, ttl=60, heartbeat_s=20)  # next regular pass: 16–20 s away
    keeper.want("x")

    async def run():
        await keeper.start()
        await asyncio.sleep(0.05)                        # first tick is inside try_acquire
        table.rows.pop("x")                              # the peer released …
        keeper.kick()                                    # … and said so
        await asyncio.sleep(0.6)
        held = keeper.holds("x")
        await keeper.stop()
        return held

    assert asyncio.run(run()) is True, "the kick was swallowed by the running tick"
    assert calls == [False, True]


# --- fencing tokens --------------------------------------------------------------------

def test_acquired_callback_gets_the_token_only_if_it_asks_for_it():
    keeper = LeaseKeeper(LocalCoordinator(instance_id="me", ttl=30), ttl=30, heartbeat_s=10)
    seen: dict = {}

    def old_style():
        seen["old"] = "called"

    def with_default(payload=None):                      # optional parameter that means something else
        seen["default"] = payload

    def named(token=None):
        seen["named"] = token

    def keyword_only(*, token):
        seen["kwonly"] = token

    def positional(fencing):
        seen["positional"] = fencing

    async def async_named(token):
        seen["async"] = token

    for cb in (old_style, with_default, named, keyword_only, positional, async_named):
        keeper.want("x", acquired=cb)

    async def run():
        await keeper.tick()
        await asyncio.sleep(0.01)

    asyncio.run(run())
    token = keeper.token("x")
    assert isinstance(token, int) and token >= 1
    assert seen == {"old": "called", "default": None, "named": token, "kwonly": token,
                    "positional": token, "async": token}
    late: list = []
    keeper.want("x", acquired=lambda token: late.append(token))   # registers on a held lease
    assert late == [token]
    assert keeper.token("not-held") is None


def test_keeper_without_a_token_driver_still_works():
    clock = FakeClock()
    table, ca, cb, ka, kb = _pair(clock)
    got: list = []
    ka.want("x", acquired=lambda token: got.append(token))
    asyncio.run(ka.tick())
    assert got == [None] and ka.token("x") is None and ka.holds("x")


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(tmp_path)
    yield eng
    eng.dispose()


def _coord(engine, iid, clock=None, ttl=30):
    coord = DbCoordinator(instance_id=iid, ttl=ttl, heartbeat_s=10, poll_ms=100, engine_getter=lambda: engine,
                          now_fn=clock)
    coord.ensure_schema()
    return coord


class DbClock:
    """Fake database clock (naive UTC) shared by several coordinators."""

    def __init__(self):
        self.now = datetime(2026, 11, 14, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_event_loop_pause_longer_than_the_ttl_fences_before_it_renews(engine):
    """A paused holder must not quietly renew a lease that had expired: the
    tick checks the deadlines first; ``holds`` is deadline-aware even before
    the ``lost`` callback ran; the re-acquisition carries a new token."""
    clock, db_clock = FakeClock(), DbClock()
    keeper = LeaseKeeper(_coord(engine, "a", db_clock, ttl=TTL), ttl=TTL, heartbeat_s=0.05, clock=clock)
    events: list = []
    keeper.want("x", acquired=lambda token: events.append(("acquired", token)),
                lost=lambda: events.append(("lost", None)))

    async def run():
        await keeper.tick()
        first = keeper.token("x")
        clock.advance(TTL + 5)                           # the loop was stalled …
        db_clock.advance(TTL + 5)                        # … while the database clock went on
        assert keeper.holds("x") is False and keeper.token("x") is None and keeper.held == []
        assert [e for e, _ in events] == ["acquired"]    # the callback has not run yet …
        await keeper.tick()                              # … it is the first thing the tick does
        return first

    first = asyncio.run(run())
    assert [e for e, _ in events] == ["acquired", "lost", "acquired"]
    assert events[2][1] > first, "re-acquisition after a pause must carry a new token"


def test_single_mode_never_fences_itself():
    """The local driver's instance is the only member: a stalled loop must not
    bounce the workers through lost → acquired."""
    clock = FakeClock()
    keeper = LeaseKeeper(LocalCoordinator(instance_id="me", ttl=TTL), ttl=TTL, heartbeat_s=0.05, clock=clock)
    events: list = []
    keeper.want("x", acquired=lambda: events.append("acquired"), lost=lambda: events.append("lost"))

    async def run():
        await keeper.tick()
        clock.advance(10 * TTL)
        assert keeper.holds("x") and keeper.fence_expired() == []
        await keeper.tick()

    asyncio.run(run())
    assert events == ["acquired"]


def test_db_driver_tokens_only_grow(engine):
    clock = DbClock()
    a, b = _coord(engine, "a", clock), _coord(engine, "b", clock)

    async def run():
        t1 = await a.acquire("x")
        assert t1 is not None and await a.acquire("x") == t1          # renewal keeps the token
        assert await b.acquire("x") is None and await b.try_acquire("x") is False
        assert (await b.holder("x")).token == t1
        clock.advance(1)
        assert await a.release("x") is True
        assert await a.holder("x") is None and await a.leases() == []   # released rows are not leases
        t2 = await b.acquire("x")                                      # the row (and its counter) survived
        assert t2 > t1
        clock.advance(31)                                              # b's lease expires
        t3 = await a.acquire("x")
        assert t3 > t2 and (await a.holder("x")).holder == "a"
        t4 = await a.acquire("x", fresh=True)                          # "I do not hold it" → new acquisition
        assert t4 > t3
        clock.advance(31)
        t5 = await a.acquire("x")                                      # own, but expired → not an unbroken holding
        assert t5 > t4
        return t5

    asyncio.run(run())


def test_db_driver_tokens_survive_older_instances(engine):
    """Mixed versions: core ≤ 1.11 releases by DELETE and takes over expired
    leases without touching the token column."""
    clock = DbClock()
    a = _coord(engine, "new-a", clock)

    async def run():
        t1 = await a.acquire("x")
        clock.advance(40)
        with engine.begin() as conn:                                   # 1.11.x takes the expired lease over
            conn.execute(update(ClusterLease).where(ClusterLease.name == "x").values(
                holder="old-b", expires_at=clock.now + timedelta(seconds=30), renewed_at=clock.now))
        assert await a.acquire("x") is None                            # held by the old instance
        clock.advance(40)
        t2 = await a.acquire("x")
        assert t2 > t1
        clock.advance(1)
        with engine.begin() as conn:                                   # 1.11.x releases: the row is gone
            conn.execute(delete(ClusterLease).where(ClusterLease.name == "x"))
        t3 = await a.acquire("x")
        assert t3 > t2, "the token must not restart when an old instance deleted the row"
        with engine.begin() as conn:                                   # 1.11.x inserts a row without a token
            conn.execute(delete(ClusterLease).where(ClusterLease.name == "x"))
            conn.execute(text(
                "INSERT INTO cluster_leases (name, holder, acquired_at, renewed_at, expires_at) "
                "VALUES ('x', 'old-b', :now, :now, :now)"), {"now": clock.now})
        clock.advance(40)
        assert await a.acquire("x") > t3

    asyncio.run(run())


def test_schema_upgrade_adds_the_token_column_to_an_existing_table(engine):
    with engine.begin() as conn:                                       # the table as core 1.11 created it
        conn.execute(text(
            "CREATE TABLE cluster_leases (name VARCHAR(150) PRIMARY KEY, holder VARCHAR(100), "
            "acquired_at DATETIME, renewed_at DATETIME, expires_at DATETIME NOT NULL)"))
        conn.execute(text(
            "INSERT INTO cluster_leases (name, holder, expires_at) VALUES ('old', 'pod-1', '2099-01-01 00:00:00')"))
    a = _coord(engine, "a")                                            # ensure_schema() upgrades in place
    _coord(engine, "b")                                                # a second instance: nothing left to do
    with engine.connect() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(cluster_leases)"))]
    assert "token" in columns

    async def run():
        assert (await a.holder("old")).token is None                   # old rows stay readable
        assert await a.acquire("old") is None                          # … and held
        assert await a.acquire("fresh") is not None

    asyncio.run(run())


def test_keeper_passes_db_tokens_and_restart_under_a_fixed_id_gets_a_new_one(engine):
    clock = DbClock()
    events: list = []

    async def incarnation():
        coord = _coord(engine, "docker-auto-runner", clock)
        keeper = LeaseKeeper(coord, ttl=30, heartbeat_s=10)
        keeper.want("iac:webhook_sync", acquired=lambda token: events.append(token))
        await keeper.tick()
        return keeper.token("iac:webhook_sync")

    first = asyncio.run(incarnation())                                 # crashes without releasing
    clock.advance(5)
    second = asyncio.run(incarnation())                                # same id, new process: row still "mine"
    assert events == [first, second] and second > first


def test_requested_ttl_below_two_heartbeats_is_raised():
    keeper = LeaseKeeper(LocalCoordinator(instance_id="me", ttl=30), ttl=30, heartbeat_s=10)
    keeper.want("short", ttl=5)
    keeper.want("fine", ttl=45)
    assert keeper._wanted["short"].ttl == 20 and keeper._wanted["fine"].ttl == 45
