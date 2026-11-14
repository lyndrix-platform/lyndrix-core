"""Per-process throttle for password logins (AZ-01).

Pure in-memory bookkeeping, no I/O: ``login_guard`` asks it before a password
is verified and tells it the outcome afterwards. Only VERIFIED failures are
counted — an attempt that was turned away never extends a block, so every
block ends by itself once the failures age out of their window.

Four budgets, each a sliding window over counted failures:

``pair``     (account, client address): 5 per minute (the long-standing
             ``/api/auth/login`` contract) and 10 per 10 minutes. Stops plain
             guessing from one address and caps what a single address can
             add to the account budget.
``account``  account, any address: 20 per 10 minutes from addresses that
             already failed on that account, plus a reserve of 10 that only
             addresses WITHOUT a failure of their own can use. Stops guessing
             spread over many addresses (botnet, forged ``X-Forwarded-For``
             behind ``LYNDRIX_TRUSTED_PROXIES=*``).
``address``  client address, any account: 30 per minute. Stops spraying one
             password over many accounts and bounds directory/DB load.
known-good   an (account, address) that produced a CORRECT password within
             the last 30 days is checked against its pair budget only.

The trade-off (who can lock whom out — see docs/security.md):

* One attacking address exhausts its own pair budget and at most half of the
  account's shared budget. Everyone else keeps logging in.
* Filling the reserve for "new" addresses takes 10 distinct fresh addresses
  every 10 minutes. Whoever can do that (botnet, spoofable client address)
  can keep ONE account's password login away from addresses that never logged
  in successfully — for as long as the attack runs plus at most 10 minutes.
  Known-good addresses, existing sessions and API keys are not affected.
  That is the price for a hard per-account ceiling on guesses; an exemption
  for every address without failures would hand the ceiling back to exactly
  the attacker who rotates addresses.
* Clients behind ONE shared address (a load balancer the core does not trust,
  NAT) share the pair budget: anybody there can block a given account's
  password login from that address while they keep failing. This is no worse
  than before and cannot be told apart server-side; the preflight warns about
  the proxy configuration.
* A client that keeps sending the SAME wrong password (a script with an
  outdated password) is counted once per 10 minutes, so it does not block the
  account for correctly configured clients on the same address.

State is per process. In a cluster every instance keeps its own counters, so
the effective budgets are multiplied by the number of instances an attacker
can reach.
TODO(agent): share the counters cluster-wide (coordinator KV) — needs a
synchronous KV accessor or an async guard for the HTTP Basic path.
"""
from __future__ import annotations

import math
import threading
import time
import unicodedata
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

PAIR_BURST_LIMIT = 5
PAIR_BURST_WINDOW = 60.0
PAIR_SUSTAINED_LIMIT = 10
# One window for everything account-related: "blocked for at most 10 minutes
# after the last counted failure" is the bound an operator can rely on.
LONG_WINDOW = 600.0
ACCOUNT_REPEAT_LIMIT = 20
ACCOUNT_FRESH_RESERVE = 10
ADDRESS_LIMIT = 30
ADDRESS_WINDOW = 60.0
KNOWN_GOOD_TTL = 30 * 86400.0
# Distinct wrong passwords remembered per account for the "same wrong password
# again" rule. A guesser cycles through more than these and is counted normally.
REMEMBERED_WRONG_PER_ACCOUNT = 8
# Memory bound per table; an attacker spraying random names must not grow them
# without limit. Expired entries go first, then the oldest.
MAX_TRACKED_KEYS = 20000
_PRUNE_EVERY = 512

# Unicode categories dropped from account names: combining marks (accents) and
# invisible format/control characters. MariaDB's default collations ignore
# them, so "ádmin" or "ad­min" would otherwise reach the "admin" row with
# a limiter bucket of their own.
_IGNORED_CATEGORIES = frozenset({"Mn", "Me", "Cf", "Cc"})


def canonical_account(username: Optional[str]) -> str:
    """Limiter key of an account name: NFKC, case-folded, accents stripped."""
    # Cut first: the name is unauthenticated input and normalisation is not free.
    text = unicodedata.normalize("NFKC", str(username or "")[:512]).strip().casefold()
    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if unicodedata.category(ch) not in _IGNORED_CATEGORIES
    )
    return unicodedata.normalize("NFKC", folded)[:128] or "?"


def _live(entries: Optional[Deque[float]], window: float, now: float) -> Deque[float]:
    """Drop timestamps older than ``window`` (in place) and return what is left."""
    if entries is None:
        return deque()
    while entries and entries[0] <= now - window:
        entries.popleft()
    return entries


def _wait_until_below(entries: Deque[float], limit: int, window: float, now: float) -> float:
    """Seconds until fewer than ``limit`` entries are inside ``window`` (0 = already)."""
    if len(entries) < limit:
        return 0.0
    # The entry whose expiry brings the count to limit - 1.
    return max(0.0, entries[len(entries) - limit] + window - now)


class LoginThrottle:
    """Thread-safe failure bookkeeping; see the module docstring for the rules."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pair: Dict[Tuple[str, str], Deque[float]] = {}
        self._account: Dict[str, Deque[float]] = {}
        self._address: Dict[str, Deque[float]] = {}
        self._known_good: Dict[Tuple[str, str], float] = {}
        self._wrong: Dict[str, Dict[str, float]] = {}  # account → {fingerprint: counted at}
        self._since_prune = 0

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    def retry_after(self, account: str, address: str) -> int:
        """Seconds until an attempt for ``account`` from ``address`` is admitted (0 = now)."""
        now = self._clock()
        with self._lock:
            pair = _live(self._pair.get((account, address)), LONG_WINDOW, now)
            recent = [t for t in pair if t > now - PAIR_BURST_WINDOW]
            wait = max(
                _wait_until_below(deque(recent), PAIR_BURST_LIMIT, PAIR_BURST_WINDOW, now),
                _wait_until_below(pair, PAIR_SUSTAINED_LIMIT, LONG_WINDOW, now),
            )
            if not self._is_known_good(account, address, now):
                wait = max(
                    wait,
                    self._account_wait(account, pair, now),
                    _wait_until_below(
                        _live(self._address.get(address), ADDRESS_WINDOW, now),
                        ADDRESS_LIMIT, ADDRESS_WINDOW, now,
                    ),
                )
        return max(1, math.ceil(wait)) if wait > 0 else 0

    def _account_wait(self, account: str, pair: Deque[float], now: float) -> float:
        failures = _live(self._account.get(account), LONG_WINDOW, now)
        fresh_limit = ACCOUNT_REPEAT_LIMIT + ACCOUNT_FRESH_RESERVE
        as_fresh = _wait_until_below(failures, fresh_limit, LONG_WINDOW, now)
        if not pair:
            return as_fresh
        # An address that failed here itself may not use the reserve — until
        # its own failures have aged out, whichever comes first.
        as_repeat = _wait_until_below(failures, ACCOUNT_REPEAT_LIMIT, LONG_WINDOW, now)
        own_failures_gone = pair[-1] + LONG_WINDOW - now
        return min(as_repeat, max(as_fresh, own_failures_gone))

    def _is_known_good(self, account: str, address: str, now: float) -> bool:
        seen = self._known_good.get((account, address))
        return seen is not None and now - seen < KNOWN_GOOD_TTL

    # ------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------

    def record_failure(self, account: str, address: str, fingerprint: Optional[str] = None) -> bool:
        """Count a verified wrong password. Returns False when it was not counted.

        ``fingerprint`` identifies the exact credentials (keyed hash, never the
        password). Passing it lets the same wrong password count only once per
        window; pass ``None`` to count unconditionally.
        """
        now = self._clock()
        with self._lock:
            if fingerprint is not None:
                seen = self._wrong.setdefault(account, {})
                for fp in [fp for fp, at in seen.items() if at <= now - LONG_WINDOW]:
                    del seen[fp]
                if fingerprint in seen:
                    return False
                if len(seen) >= REMEMBERED_WRONG_PER_ACCOUNT:
                    del seen[next(iter(seen))]  # oldest first (insertion order)
                seen[fingerprint] = now

            self._append(self._pair, (account, address), now)
            if not self._is_known_good(account, address, now):
                # Failures of a known-good pair are bounded by its own budget
                # and must not use up the budgets that protect everyone else.
                self._append(self._account, account, now)
                self._append(self._address, address, now)
            self._since_prune += 1
            if self._since_prune >= _PRUNE_EVERY or self._oversized():
                self._prune(now)
        return True

    def record_unevaluated(self, address: str) -> None:
        """Count an attempt nobody could judge (auth backend unreachable).

        It only uses up the ADDRESS budget — which bounds the load one client
        can put on a struggling backend — and never an account's: after an
        outage the people who kept retrying must not find themselves blocked.
        """
        now = self._clock()
        with self._lock:
            self._append(self._address, address, now)

    def record_success(self, account: str, address: str) -> None:
        """Remember that ``address`` produced the correct password for ``account``.

        Failures are NOT cleared: otherwise every successful request of a
        legitimate client would hand a fresh budget to a guesser on the same
        address.
        """
        now = self._clock()
        with self._lock:
            self._known_good[(account, address)] = now
            if len(self._known_good) > MAX_TRACKED_KEYS:
                self._prune(now)

    def reset(self) -> None:
        """Forget everything (tests; operator action after a false block)."""
        with self._lock:
            self._pair.clear()
            self._account.clear()
            self._address.clear()
            self._known_good.clear()
            self._wrong.clear()
            self._since_prune = 0

    # ------------------------------------------------------------------
    # Memory bound
    # ------------------------------------------------------------------

    @staticmethod
    def _append(table: dict, key, now: float) -> None:
        # Re-insert so the dict stays ordered by LAST failure: when the table
        # overflows, the keys that have been quiet longest are dropped first.
        entries = table.pop(key, None)
        if entries is None:
            entries = deque()
        entries.append(now)
        table[key] = entries

    def _oversized(self) -> bool:
        return any(
            len(table) > MAX_TRACKED_KEYS
            for table in (self._pair, self._account, self._address, self._wrong)
        )

    def _prune(self, now: float) -> None:
        self._since_prune = 0
        for table, window in (
            (self._pair, LONG_WINDOW),
            (self._account, LONG_WINDOW),
            (self._address, ADDRESS_WINDOW),
        ):
            for key in [k for k, entries in table.items() if not _live(entries, window, now)]:
                del table[key]
        for account in [
            a for a, seen in self._wrong.items()
            if all(at <= now - LONG_WINDOW for at in seen.values())
        ]:
            del self._wrong[account]
        for key in [k for k, at in self._known_good.items() if now - at >= KNOWN_GOOD_TTL]:
            del self._known_good[key]
        # Still too big (a flood of distinct names inside one window): drop the
        # keys that have been quiet longest. Lifting somebody's block this way
        # costs a flood of MAX_TRACKED_KEYS counted failures for a few guesses.
        for table in (self._pair, self._account, self._address, self._wrong, self._known_good):
            excess = len(table) - MAX_TRACKED_KEYS
            if excess > 0:
                for key in list(table)[:excess]:
                    del table[key]


# The one throttle of this process.
login_throttle = LoginThrottle()
