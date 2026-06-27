"""
Simple in-memory sliding-window rate limiter.

Used to throttle security-sensitive Vault operations (init / unseal) per source
identity (client IP or NiceGUI client id). Keeping the state keyed per source
avoids the cross-session lockout problem of a single global counter.
"""

import time
from collections import defaultdict


class SlidingWindowRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def _purge(self, key: str, now: float) -> None:
        self._attempts[key] = [
            ts for ts in self._attempts[key] if now - ts < self.window_seconds
        ]

    def retry_after(self, key: str) -> int:
        """Return seconds until the next attempt is allowed (0 when allowed)."""
        now = time.time()
        self._purge(key, now)
        if len(self._attempts[key]) >= self.max_attempts:
            return max(1, int(self.window_seconds - (now - self._attempts[key][0])))
        return 0

    def record(self, key: str) -> None:
        """Record an attempt for *key*."""
        self._attempts[key].append(time.time())

    def hit(self, key: str) -> int:
        """
        Combined check + record: if the source is within budget, record the
        attempt and return 0; otherwise return the retry-after seconds without
        recording.
        """
        retry = self.retry_after(key)
        if retry > 0:
            return retry
        self.record(key)
        return 0


# Shared limiter for Vault unseal/init attempts (per source identity).
unseal_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60)
