"""
Simple in-memory sliding-window rate limiter.

Used to throttle security-sensitive Vault operations (init / unseal) per source
identity (client IP or NiceGUI client id). Keeping the state keyed per source
avoids the cross-session lockout problem of a single global counter.
"""

import ipaddress
import time
from collections import defaultdict
from typing import Optional


def normalize_client_address(host: Optional[str]) -> str:
    """Limiter key for a client address (``request.client.host``).

    * IPv6 is reduced to its /64 network: one subscriber controls a whole /64,
      so per-address budgets keyed on single addresses would be free to dodge.
    * An IPv4-mapped IPv6 address counts as the IPv4 address.
    * Anything that is not an IP address (unix socket, test client) is kept as
      an opaque, length-bounded name; a missing address is ``"unknown"``.

    The address is whatever uvicorn resolved from the trusted proxies
    (``LYNDRIX_TRUSTED_PROXIES``); this function never looks at headers itself.
    """
    raw = (host or "").strip()
    if not raw:
        return "unknown"
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw[:64].lower()
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network((ip, 64), strict=False))
    return str(ip)


class SlidingWindowRateLimiter:
    _MAX_KEYS = 10000

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
        now = time.time()
        self._attempts[key].append(now)
        if len(self._attempts) > self._MAX_KEYS:
            # Keys are client-chosen (addresses): forget the idle ones so a
            # caller rotating them cannot grow this table without bound.
            for stale in [k for k, ts in self._attempts.items() if not ts or now - ts[-1] >= self.window_seconds]:
                del self._attempts[stale]
            for oldest in list(self._attempts)[: max(0, len(self._attempts) - self._MAX_KEYS)]:
                del self._attempts[oldest]

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
