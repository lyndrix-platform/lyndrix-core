"""The one choke point for password logins (AZ-01).

Every place that checks a username + password goes through here:

* ``POST /api/auth/login`` and the NiceGUI login form — the provider chain
  (local, LDAP, …) via ``provider_registry.authenticate``;
* HTTP Basic on API routes and "confirm your current password" checks — local
  accounts via ``auth_service.verify_local_login``.

Two layers, applied in this order:

1. **Throttle** (``guarded_verify`` / ``guarded_verify_async``): ask
   ``login_throttle`` whether the attempt is admitted, run the verification,
   report the outcome. One call = one counted attempt.
2. **Bounded verification** (``verify_login_password``): compare a password
   with a stored Argon2 hash under a process-wide concurrency limit, with a
   short memory of verdicts so that a client presenting the same credentials
   on every request (HTTP Basic) costs one Argon2 run, not one per request.

Everything is synchronous and in-memory except the Argon2 run itself; async
callers run ``verify_login_password`` in a worker thread.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Awaitable, Callable, Dict, Optional, Tuple, TypeVar

from config import settings
from core.logger import get_logger
from core.components.vault.logic.rate_limit import normalize_client_address
from .hashing import verify_password
from .login_throttle import (
    LONG_WINDOW,
    PAIR_BURST_LIMIT,
    PAIR_BURST_WINDOW,
    canonical_account,
    login_throttle,
)

log = get_logger("Core:LoginGuard")

T = TypeVar("T")


class LoginRejected(Exception):
    """The attempt was NOT evaluated — there is no verdict on the credentials.

    Callers answer with ``status_code`` + ``Retry-After`` instead of 401: a
    client must not conclude that its password is wrong.
    """

    status_code = 429

    def __init__(self, detail: str, retry_after: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retry_after = max(1, int(retry_after))


class LoginThrottled(LoginRejected):
    """Too many failed attempts for this account / from this address (429)."""

    status_code = 429


class LoginBusy(LoginRejected):
    """Every verification slot is taken and the wait ran out (503)."""

    status_code = 503


# ──────────────────────────────────────────────────────────────────────────────
# Layer 2 — bounded Argon2 verification
# ──────────────────────────────────────────────────────────────────────────────

# Keys the in-memory fingerprints below. Never leaves the process, so a
# fingerprint is useless outside of it and changes with every restart.
_PROCESS_KEY = secrets.token_bytes(32)

# How long a caller waits for a free slot before it is told to come back.
_SLOT_WAIT_SECONDS = 5.0
# Callers allowed to wait per slot. Beyond that the answer is "busy" at once:
# waiting callers hold worker threads the rest of the API needs.
_WAITERS_PER_SLOT = 6
# A confirmed password stays confirmed while it keeps being used (HTTP Basic
# sends it with every request). The entry is bound to the stored hash, so a
# password change, a removed password and a deleted account miss the cache.
_CONFIRMED_IDLE_SECONDS = 300.0
# A rejected password stays rejected for one throttle window: a script that
# repeats an outdated password costs one Argon2 run per window.
_REJECTED_SECONDS = LONG_WINDOW
_CACHE_MAX_ENTRIES = 4096


def _keyed_digest(*parts: str) -> bytes:
    mac = hmac.new(_PROCESS_KEY, digestmod=hashlib.sha256)
    for part in parts:
        mac.update(part.encode("utf-8", "surrogatepass"))
        mac.update(b"\x00")
    return mac.digest()


class _BoundedVerifier:
    """Argon2 verification with a concurrency limit and a verdict memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: Optional[threading.BoundedSemaphore] = None
        self._max_waiting = 0
        self._waiting = 0
        self._verdicts: Dict[bytes, Tuple[bool, float]] = {}

    def _semaphore(self) -> threading.BoundedSemaphore:
        with self._lock:
            if self._slots is None:
                slots = max(1, int(getattr(settings, "LYNDRIX_LOGIN_VERIFY_CONCURRENCY", 4) or 1))
                self._slots = threading.BoundedSemaphore(slots)
                self._max_waiting = slots * _WAITERS_PER_SLOT
            return self._slots

    def _remembered(self, key: bytes, now: float) -> Optional[bool]:
        with self._lock:
            entry = self._verdicts.get(key)
            if entry is None:
                return None
            verdict, expires = entry
            if expires <= now:
                del self._verdicts[key]
                return None
            if verdict:
                self._verdicts[key] = (True, now + _CONFIRMED_IDLE_SECONDS)
            return verdict

    def _remember(self, key: bytes, verdict: bool, now: float) -> None:
        ttl = _CONFIRMED_IDLE_SECONDS if verdict else _REJECTED_SECONDS
        with self._lock:
            if len(self._verdicts) >= _CACHE_MAX_ENTRIES:
                for stale in [k for k, (_v, exp) in self._verdicts.items() if exp <= now]:
                    del self._verdicts[stale]
                while len(self._verdicts) >= _CACHE_MAX_ENTRIES:
                    del self._verdicts[next(iter(self._verdicts))]
            self._verdicts[key] = (verdict, now + ttl)

    def verify(self, stored_hash: Optional[str], password: str) -> bool:
        if not stored_hash:
            return False  # no local password (SSO/LDAP-only account)
        key = _keyed_digest(stored_hash, password)
        remembered = self._remembered(key, time.monotonic())
        if remembered is not None:
            return remembered

        slots = self._semaphore()
        with self._lock:
            if self._waiting >= self._max_waiting:
                raise LoginBusy("The server is busy verifying logins. Try again shortly.", 2)
            self._waiting += 1
        try:
            acquired = slots.acquire(timeout=_SLOT_WAIT_SECONDS)
        finally:
            with self._lock:
                self._waiting -= 1
        if not acquired:
            raise LoginBusy("The server is busy verifying logins. Try again shortly.", 2)
        try:
            # Somebody may have verified the same credentials while we waited
            # (a burst of requests of one client): use that verdict.
            remembered = self._remembered(key, time.monotonic())
            if remembered is not None:
                return remembered
            verdict = bool(verify_password(stored_hash, password))
            self._remember(key, verdict, time.monotonic())
            return verdict
        finally:
            slots.release()

    def reset(self) -> None:
        with self._lock:
            self._verdicts.clear()
            self._slots = None
            self._waiting = 0


_verifier = _BoundedVerifier()


def verify_login_password(stored_hash: Optional[str], password: str) -> bool:
    """Compare ``password`` with ``stored_hash`` (blocking — worker thread only).

    Raises ``LoginBusy`` when no verification slot frees up in time. Does NOT
    throttle: call it inside ``guarded_verify`` / ``guarded_verify_async``.
    """
    return _verifier.verify(stored_hash, password)


# ──────────────────────────────────────────────────────────────────────────────
# Layer 1 — throttle
# ──────────────────────────────────────────────────────────────────────────────

# One "throttled" log line per (account, address) and minute: the log (and the
# shared cluster log table behind it) must not become the flood's amplifier.
_THROTTLE_LOG_INTERVAL = 60.0
_throttle_logged: Dict[Tuple[str, str], float] = {}


def client_host(request) -> Optional[str]:
    """``request.client.host`` or ``None`` (synthetic requests have no client)."""
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client is not None else None


def _admit(username: str, client_ip: Optional[str], local_username: Optional[str]) -> Tuple[str, str]:
    """Throttle keys of an admitted attempt; raises ``LoginThrottled`` otherwise.

    ``local_username`` is the stored name of the local account the typed name
    resolves to (the database compares case- and accent-insensitively), so
    every spelling of one account shares one bucket.
    """
    account = canonical_account(local_username or username)
    address = normalize_client_address(client_ip)
    retry = login_throttle.retry_after(account, address)
    if retry > 0:
        now = time.monotonic()
        if now - _throttle_logged.get((account, address), -_THROTTLE_LOG_INTERVAL) >= _THROTTLE_LOG_INTERVAL:
            if len(_throttle_logged) > 2048:
                _throttle_logged.clear()
            _throttle_logged[(account, address)] = now
            log.warning(
                f"AUTH: password logins for {account!r} from {address} are throttled "
                f"(too many failed attempts) — next attempt in {retry}s."
            )
        raise LoginThrottled("Too many failed login attempts. Try again later.", retry)
    return account, address


class _Unevaluated:
    """Marker ``verify`` returns when NOBODY could judge the credentials."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<login attempt not evaluated>"


# Returned by ``verify`` instead of ``None`` when the account is not a local
# one with a password and an auth backend was unreachable: the login is denied,
# but it is no wrong password. See ``LoginThrottle.record_unevaluated``.
UNEVALUATED = _Unevaluated()


def _settle(account: str, address: str, password: str, outcome, *, count_repeats: bool) -> bool:
    """Report the outcome to the throttle; True when a failure was counted."""
    if outcome is UNEVALUATED:
        login_throttle.record_unevaluated(address)
        return False
    if outcome:
        login_throttle.record_success(account, address)
        return False
    # The fingerprint lets the SAME wrong password count once per window
    # (outdated credentials in a script). When an external directory was asked
    # every failure counts: each one was a bind against that directory.
    fingerprint = None if count_repeats else _keyed_digest(account, password).hex()
    return login_throttle.record_failure(account, address, fingerprint)


# The login form's (account, address) burst counter has been shared between
# the instances of a cluster through the coordinator KV since 1.4; it is kept
# so the form's "5 failures per minute" stays a cluster-wide limit. Same key
# format as before, so instances of different versions share it during a
# rolling update. Everything else in login_throttle is per process.
_wall_clock = time.time  # module attribute so tests can move the window


def _shared_bucket(account: str, address: str) -> Tuple[str, int]:
    now = int(_wall_clock())
    window = int(PAIR_BURST_WINDOW)
    window_start = now - now % window
    bucket = f"{account}|{address}"
    if len(bucket) > 120:
        # cluster_kv keys are VARCHAR(190); a long (attacker-chosen) name is
        # replaced by its digest so the counter still works for it.
        bucket = "h:" + hashlib.sha256(bucket.encode("utf-8", "surrogatepass")).hexdigest()[:48]
    return f"login:fail:{bucket}:{window_start}", window - (now - window_start)


async def _shared_retry_after(account: str, address: str) -> int:
    """Seconds left in the cluster-wide burst window when it is used up, else 0.

    A KV error answers 0: the per-process throttle has already admitted the
    attempt, and a database hiccup must not lock everybody out of the form.
    """
    from core.components.cluster.logic.cluster_service import cluster_service

    key, remaining = _shared_bucket(account, address)
    try:
        count = int(await cluster_service.kv_get(key) or 0)
    except Exception:
        return 0
    return max(1, remaining) if count >= PAIR_BURST_LIMIT else 0


async def _shared_record_failure(account: str, address: str) -> None:
    from core.components.cluster.logic.cluster_service import cluster_service

    key, remaining = _shared_bucket(account, address)
    try:
        await cluster_service.kv_incr(key, ttl=max(1, remaining))
    except Exception:
        pass  # counted per process already


def guarded_verify(
    username: str,
    password: str,
    client_ip: Optional[str],
    verify: Callable[[], Optional[T]],
    *,
    local_username: Optional[str] = None,
    external: bool = False,
) -> Optional[T]:
    """Run ``verify`` as ONE throttled login attempt (synchronous callers).

    ``verify`` returns something truthy for correct credentials, ``None`` for
    wrong ones and ``UNEVALUATED`` when they could not be judged. Raises
    ``LoginThrottled`` before verifying when the attempt is not admitted and
    lets ``LoginBusy`` (or any other error) from ``verify`` through — neither
    is a verdict, so nothing is counted.
    """
    account, address = _admit(username, client_ip, local_username)
    outcome = verify()
    _settle(account, address, password, outcome, count_repeats=external)
    return outcome or None


async def guarded_verify_async(
    username: str,
    password: str,
    client_ip: Optional[str],
    verify: Callable[[], Awaitable[Optional[T]]],
    *,
    local_username: Optional[str] = None,
    external: bool = False,
) -> Optional[T]:
    """``guarded_verify`` for the async provider chain (the login forms).

    Additionally honours the cluster-wide burst counter of the login form.
    """
    account, address = _admit(username, client_ip, local_username)
    shared_retry = await _shared_retry_after(account, address)
    if shared_retry > 0:
        raise LoginThrottled("Too many failed login attempts. Try again later.", shared_retry)
    outcome = await verify()
    if _settle(account, address, password, outcome, count_repeats=external):
        await _shared_record_failure(account, address)
    return outcome or None


def resolve_local_account(username: str) -> Tuple[Optional[str], bool]:
    """``(stored name, has a local password)`` of the local account that
    ``username`` resolves to (blocking).

    ``(None, False)`` when there is no such account or the database is not
    available — the typed name is then the throttle key.
    """
    from core.components.database.logic.db_service import db_instance
    from .models import User

    if not db_instance.SessionLocal or not username:
        return None, False
    try:
        with db_instance.SessionLocal() as session:
            row = (
                session.query(User.username, User.hashed_password)
                .filter(User.username == username)
                .first()
            )
            return (str(row[0]), bool(row[1])) if row else (None, False)
    except Exception as exc:
        log.debug(f"AUTH: could not resolve the local account for a login attempt: {exc}")
        return None, False


def reset_for_tests() -> None:
    """Forget throttle state and remembered verdicts."""
    login_throttle.reset()
    _verifier.reset()
    _throttle_logged.clear()
