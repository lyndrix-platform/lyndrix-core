"""AZ-01 — the login throttle's budgets and who can lock whom out.

Pure bookkeeping tests on a fake clock (no app, no database).
"""
import pytest

from core.components.auth.logic import login_throttle as lt
from core.components.auth.logic.login_throttle import LoginThrottle, canonical_account
from core.components.vault.logic.rate_limit import normalize_client_address


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def throttle(clock):
    return LoginThrottle(clock=clock)


def _fail(throttle, account, address, times=1, clock=None, spacing=0.0):
    for _ in range(times):
        assert throttle.retry_after(account, address) == 0, "attempt should have been admitted"
        throttle.record_failure(account, address)
        if clock is not None and spacing:
            clock.advance(spacing)


# ── keys ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", [
    "admin", "ADMIN", " Admin ", "ádmin", "àdmin", "âdmin", "ädmin", "Ａｄｍｉｎ",
    "ad­min",          # soft hyphen (ignorable in UCA collations)
    "a​dmin",          # zero-width space
    "ádmin",          # combining acute
])
def test_every_spelling_the_database_accepts_shares_one_bucket(spelling):
    assert canonical_account(spelling) == "admin"


def test_distinct_names_keep_distinct_buckets():
    assert canonical_account("admin2") != canonical_account("admin")
    assert canonical_account("") == canonical_account(None) == "?"
    assert len(canonical_account("x" * 500)) == 128


def test_client_addresses_are_normalised():
    assert normalize_client_address("10.1.2.3") == "10.1.2.3"
    assert normalize_client_address("::ffff:10.1.2.3") == "10.1.2.3"
    # One subscriber owns a whole /64 — rotating inside it must not help.
    assert normalize_client_address("2001:db8:1:2:aaaa::1") == normalize_client_address("2001:db8:1:2:bbbb::2")
    assert normalize_client_address("2001:db8:1:3::1") != normalize_client_address("2001:db8:1:2::1")
    assert normalize_client_address(None) == normalize_client_address("") == "unknown"
    assert normalize_client_address("TestClient") == "testclient"


# ── pair budget (account + address) ─────────────────────────────────────────


def test_five_failures_a_minute_then_blocked_until_they_age_out(throttle, clock):
    _fail(throttle, "admin", "10.0.0.1", times=5)
    retry = throttle.retry_after("admin", "10.0.0.1")
    assert 1 <= retry <= 60
    clock.advance(retry)
    assert throttle.retry_after("admin", "10.0.0.1") == 0


def test_rejected_attempts_do_not_extend_the_block(throttle, clock):
    _fail(throttle, "admin", "10.0.0.1", times=5)
    for _ in range(200):                      # hammering while blocked
        assert throttle.retry_after("admin", "10.0.0.1") > 0
        clock.advance(0.1)
    clock.advance(41)                         # 61 s after the failures
    assert throttle.retry_after("admin", "10.0.0.1") == 0


def test_sustained_guessing_from_one_address_is_capped_at_ten_per_ten_minutes(throttle, clock):
    counted = 0
    for _ in range(600):                      # one attempt per second for 10 minutes
        if throttle.retry_after("admin", "10.0.0.1") == 0:
            throttle.record_failure("admin", "10.0.0.1")
            counted += 1
        clock.advance(1)
    assert counted == lt.PAIR_SUSTAINED_LIMIT
    # ...and the block is bounded: it ends within one window after the last counted failure.
    clock.advance(lt.LONG_WINDOW)
    assert throttle.retry_after("admin", "10.0.0.1") == 0


def test_other_accounts_and_addresses_are_unaffected(throttle):
    _fail(throttle, "admin", "10.0.0.1", times=5)
    assert throttle.retry_after("viewer", "10.0.0.1") == 0
    assert throttle.retry_after("admin", "10.0.0.2") == 0


def test_a_success_does_not_hand_back_the_budget(throttle):
    """Otherwise each request of a legitimate client on a shared address would
    reset the counter for a guesser on the same address."""
    _fail(throttle, "bot", "10.0.0.1", times=4)
    throttle.record_success("bot", "10.0.0.1")
    throttle.record_failure("bot", "10.0.0.1")
    assert throttle.retry_after("bot", "10.0.0.1") > 0


# ── the same wrong password again ───────────────────────────────────────────


def test_repeating_one_wrong_password_counts_once(throttle, clock):
    """A script with an outdated password must not block the account for the
    correctly configured client next to it."""
    for _ in range(500):
        assert throttle.retry_after("bot", "10.0.0.1") == 0
        throttle.record_failure("bot", "10.0.0.1", fingerprint="stale-password")
        clock.advance(1)
    assert throttle.retry_after("bot", "10.0.0.1") == 0


def test_distinct_wrong_passwords_are_each_counted(throttle):
    for i in range(5):
        assert throttle.record_failure("bot", "10.0.0.1", fingerprint=f"guess-{i}") is True
    assert throttle.retry_after("bot", "10.0.0.1") > 0


def test_cycling_through_more_guesses_than_are_remembered_is_counted(throttle, clock):
    guesses = [f"g{i}" for i in range(lt.REMEMBERED_WRONG_PER_ACCOUNT + 1)]
    counted = 0
    for _round in range(3):
        for g in guesses:
            if throttle.retry_after("bot", "10.0.0.1") == 0:
                counted += throttle.record_failure("bot", "10.0.0.1", fingerprint=g)
            clock.advance(13)                        # slow enough to stay under the burst limit
    assert counted == lt.PAIR_SUSTAINED_LIMIT        # throttled like any guesser


# ── account budget (any address) ────────────────────────────────────────────


def _exhaust_pair(throttle, clock, account, address):
    """Let one address use up its sustained pair budget (10 failures)."""
    _fail(throttle, account, address, times=5)
    clock.advance(61)
    _fail(throttle, account, address, times=5)


def test_one_attacking_address_cannot_lock_the_account(throttle, clock):
    _exhaust_pair(throttle, clock, "admin", "203.0.113.1")
    assert throttle.retry_after("admin", "203.0.113.1") > 0          # the attacker is blocked…
    assert throttle.retry_after("admin", "10.0.0.5") == 0            # …a fresh address is not
    # A legitimate user who mistypes once on another address is not blocked either.
    throttle.record_failure("admin", "10.0.0.5")
    assert throttle.retry_after("admin", "10.0.0.5") == 0


def test_addresses_that_failed_share_twenty_per_window_fresh_ones_keep_a_reserve(throttle, clock):
    _exhaust_pair(throttle, clock, "admin", "203.0.113.1")
    _exhaust_pair(throttle, clock, "admin", "203.0.113.2")           # 20 failures on the account
    # A third attacking address gets ONE attempt (as a fresh address), then nothing:
    assert throttle.retry_after("admin", "203.0.113.3") == 0
    throttle.record_failure("admin", "203.0.113.3")
    assert throttle.retry_after("admin", "203.0.113.3") > 0
    # A legitimate user on an address without failures still gets in.
    assert throttle.retry_after("admin", "10.0.0.5") == 0


def test_rotating_addresses_hits_a_hard_ceiling_per_account(throttle, clock):
    """Forged X-Forwarded-For / a botnet: every attempt from a new address."""
    counted = 0
    for i in range(500):
        address = f"198.51.{i // 250}.{i % 250}"
        if throttle.retry_after("admin", address) == 0:
            throttle.record_failure("admin", address)
            counted += 1
        clock.advance(0.5)
    assert counted == lt.ACCOUNT_REPEAT_LIMIT + lt.ACCOUNT_FRESH_RESERVE
    retry = throttle.retry_after("admin", "10.9.9.9")
    assert 0 < retry <= lt.LONG_WINDOW                               # bounded, never "forever"
    clock.advance(lt.LONG_WINDOW)
    assert throttle.retry_after("admin", "10.9.9.9") == 0


def test_a_known_good_address_keeps_working_while_the_account_is_under_attack(throttle, clock):
    throttle.record_success("admin", "10.0.0.5")                     # the user logged in from here before
    for i in range(200):
        address = f"198.51.100.{i}"
        if throttle.retry_after("admin", address) == 0:
            throttle.record_failure("admin", address)
    assert throttle.retry_after("admin", "10.9.9.9") > 0             # unknown address: blocked
    assert throttle.retry_after("admin", "10.0.0.5") == 0            # known-good: only its own budget
    # Its own typos are still limited by the pair budget.
    for _ in range(5):
        throttle.record_failure("admin", "10.0.0.5")
    assert throttle.retry_after("admin", "10.0.0.5") > 0


def test_a_mistyping_user_is_blocked_for_at_most_one_window_during_an_attack(throttle, clock):
    _exhaust_pair(throttle, clock, "admin", "203.0.113.1")
    _exhaust_pair(throttle, clock, "admin", "203.0.113.2")
    throttle.record_failure("admin", "10.0.0.5")                     # one typo while 20 are on record
    retry = throttle.retry_after("admin", "10.0.0.5")
    assert 0 < retry <= lt.LONG_WINDOW
    clock.advance(retry)
    assert throttle.retry_after("admin", "10.0.0.5") == 0


# ── address budget (any account) ────────────────────────────────────────────


def test_spraying_accounts_from_one_address_is_capped(throttle, clock):
    counted = 0
    for i in range(100):
        if throttle.retry_after(f"user{i}", "203.0.113.9") == 0:
            throttle.record_failure(f"user{i}", "203.0.113.9")
            counted += 1
    assert counted == lt.ADDRESS_LIMIT
    assert 0 < throttle.retry_after("someone-else", "203.0.113.9") <= lt.ADDRESS_WINDOW
    assert throttle.retry_after("someone-else", "10.0.0.5") == 0
    clock.advance(lt.ADDRESS_WINDOW)
    assert throttle.retry_after("someone-else", "203.0.113.9") == 0


def test_known_good_clients_behind_a_flooded_shared_address_keep_working(throttle):
    """All clients behind one load-balancer address: the address budget is
    shared, but an account that logged in successfully from there is exempt."""
    throttle.record_success("alice", "10.0.0.1")
    for i in range(lt.ADDRESS_LIMIT):
        throttle.record_failure(f"junk{i}", "10.0.0.1")
    assert throttle.retry_after("bob", "10.0.0.1") > 0
    assert throttle.retry_after("alice", "10.0.0.1") == 0


def test_unevaluated_attempts_use_the_address_budget_only(throttle, clock):
    """Directory down: nobody's password was judged — no account may end up blocked."""
    for _ in range(lt.ADDRESS_LIMIT):
        assert throttle.retry_after("ldapuser", "10.0.0.1") == 0
        throttle.record_unevaluated("10.0.0.1")
    # The address has used its budget (load protection for the backend)…
    assert throttle.retry_after("ldapuser", "10.0.0.1") > 0
    clock.advance(lt.ADDRESS_WINDOW)
    # …but one minute later nothing is left over: no pair, no account failures.
    assert throttle.retry_after("ldapuser", "10.0.0.1") == 0
    assert not throttle._pair and not throttle._account


# ── memory bound ────────────────────────────────────────────────────────────


def test_tables_stay_bounded_under_a_flood_of_names(throttle, clock, monkeypatch):
    monkeypatch.setattr(lt, "MAX_TRACKED_KEYS", 200)
    for i in range(2000):
        throttle.record_failure(f"name{i}", f"198.51.{i % 200}.{i % 250}", fingerprint=f"f{i}")
        throttle.record_success(f"name{i}", "10.0.0.1")
    for table in (throttle._pair, throttle._account, throttle._address, throttle._wrong,
                  throttle._known_good):
        assert len(table) <= 200 + lt._PRUNE_EVERY


def test_reset_forgets_everything(throttle):
    _fail(throttle, "admin", "10.0.0.1", times=5)
    throttle.reset()
    assert throttle.retry_after("admin", "10.0.0.1") == 0
