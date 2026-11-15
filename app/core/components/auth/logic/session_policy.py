"""Lifetime policy of login sessions (``LYNDRIX_SESSION_MAX_AGE_HOURS``).

Login sessions got an absolute lifetime with core 1.12. A deployment that
upgrades already holds sessions older than that lifetime — browser logins, but
also session tokens operators copied into scripts. Expiring them on the first
request of the first upgraded instance would sign everyone out in the middle of
the rollout, and because the database is shared it would also break the
instances that still run the previous version.

So the deployment records ONCE the instant at which the rule began to apply
(``auth_markers`` row ``session_max_age_enforced_from``, written under the
bootstrap lock by the first instance that knows the rule). A session created
before that instant is a *legacy* session and ``LYNDRIX_SESSION_MAX_AGE_LEGACY``
says what happens to it:

* ``exempt`` (default)   — it never expires by age;
* ``from_enforcement``   — its age counts from the recorded instant, so all
  legacy sessions end together one max-age after the upgrade;
* ``strict``             — it is treated like any other session (age counts
  from its creation; old ones end with their next request).

The exemption concerns the AGE only: revoking a session, changing the password
or deleting the account ends a legacy session like any other.

The decision is a pure function of the session row, the two settings and the
recorded instant — nothing an instance keeps for itself. The instant is
immutable once written, which is why caching a successful read is safe. While
it cannot be read (table not created yet, database hiccup) the caller gets
"unknown" and must not expire anything.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import AuthMarker

log = get_logger("Core:SessionPolicy")

MARKER_NAME = "session_max_age_enforced_from"

LEGACY_EXEMPT = "exempt"
LEGACY_FROM_ENFORCEMENT = "from_enforcement"
LEGACY_STRICT = "strict"
LEGACY_MODES = (LEGACY_EXEMPT, LEGACY_FROM_ENFORCEMENT, LEGACY_STRICT)

# A failed read of the instant is not repeated on every request.
_RETRY_UNREADABLE_S = 10.0

_lock = threading.Lock()
# (engine the value was read from, instant). Bound to the engine so a
# reconnect to another database (and every test database) reads afresh.
_cached: Optional[Tuple[object, datetime]] = None
_unreadable_until = 0.0
_unreadable_logged = False


def max_age() -> Optional[timedelta]:
    """Absolute lifetime of a login session, or ``None`` for "never expires"
    (``LYNDRIX_SESSION_MAX_AGE_HOURS``, 0 = never)."""
    try:
        from config import settings

        hours = int(getattr(settings, "LYNDRIX_SESSION_MAX_AGE_HOURS", 0) or 0)
    except Exception:  # pragma: no cover - defensive
        hours = 0
    return timedelta(hours=hours) if hours > 0 else None


def legacy_mode() -> str:
    """Normalised ``LYNDRIX_SESSION_MAX_AGE_LEGACY`` (unknown → ``exempt``, the
    value that never signs anyone out; ``Settings`` rejects unknown values at
    start, this only covers a value patched in at runtime)."""
    try:
        from config import settings

        mode = str(getattr(settings, "LYNDRIX_SESSION_MAX_AGE_LEGACY", LEGACY_EXEMPT) or "").strip().lower()
    except Exception:  # pragma: no cover - defensive
        mode = LEGACY_EXEMPT
    return mode if mode in LEGACY_MODES else LEGACY_EXEMPT


def ensure_enforcement_start() -> Optional[datetime]:
    """Record the enforcement instant if the deployment has none yet.

    Idempotent and safe against a peer doing the same at the same moment: the
    marker name is the primary key, so exactly one INSERT wins and everybody
    returns the committed row. Called from the IAM bootstrap (under the
    ``lyndrix_bootstrap`` lock, after ``create_all``). Returns ``None`` when the
    instant could neither be written nor read — sessions then do not expire by
    age until a later boot succeeds.
    """
    from sqlalchemy.exc import IntegrityError

    if db_instance.SessionLocal is None:
        return None
    try:
        with db_instance.SessionLocal() as s:
            row = s.get(AuthMarker, MARKER_NAME)
            if row is None:
                s.add(AuthMarker(name=MARKER_NAME, at=datetime.utcnow()))
                try:
                    s.commit()
                    log.info(
                        "SESSIONS: login-session max-age applies from now on; sessions created "
                        "earlier follow LYNDRIX_SESSION_MAX_AGE_LEGACY "
                        f"(currently '{legacy_mode()}')."
                    )
                except IntegrityError:
                    s.rollback()  # a peer instance recorded it first — use its value
                row = s.get(AuthMarker, MARKER_NAME)
            instant = row.at if row is not None else None
    except Exception as exc:
        log.warning(f"SESSIONS: could not record the max-age enforcement start: {exc}")
        return None
    if instant is not None:
        _remember(instant)
    return instant


def enforcement_start() -> Optional[datetime]:
    """The recorded enforcement instant, or ``None`` while it is unknown.

    Never raises and never writes. Reads on a session of its own, so a missing
    table cannot break the transaction of the caller.
    """
    global _unreadable_until, _unreadable_logged
    engine = db_instance.engine
    cached = _cached
    if cached is not None and cached[0] is engine:
        return cached[1]
    if db_instance.SessionLocal is None:
        return None
    with _lock:
        if time.monotonic() < _unreadable_until:
            return None
    instant: Optional[datetime] = None
    try:
        with db_instance.SessionLocal() as s:
            row = s.get(AuthMarker, MARKER_NAME)
            instant = row.at if row is not None else None
    except Exception as exc:
        if not _unreadable_logged:
            _unreadable_logged = True
            log.warning(
                f"SESSIONS: max-age enforcement start is not readable ({exc}) — "
                "login sessions do not expire by age until it is."
            )
    if instant is None:
        with _lock:
            _unreadable_until = time.monotonic() + _RETRY_UNREADABLE_S
        return None
    _remember(instant)
    return instant


def _remember(instant: datetime) -> None:
    global _cached, _unreadable_until, _unreadable_logged
    with _lock:
        _cached = (db_instance.engine, instant)
        _unreadable_until = 0.0
        _unreadable_logged = False


def reset_for_tests() -> None:
    global _cached, _unreadable_until, _unreadable_logged
    with _lock:
        _cached = None
        _unreadable_until = 0.0
        _unreadable_logged = False


@dataclass(frozen=True)
class SessionLifetime:
    """What the policy says about one login session."""

    # When the session ends by age; ``None`` = it does not (see ``reason``).
    expires_at: Optional[datetime]
    # Created before the enforcement instant.
    legacy: bool
    # "max_age" | "legacy_exempt" | "legacy_from_enforcement" | "disabled"
    # | "unknown" (the enforcement instant is not readable — do not expire).
    reason: str

    def expired(self, now: Optional[datetime] = None) -> bool:
        return self.expires_at is not None and self.expires_at < (now or datetime.utcnow())


def lifetime(created_at: Optional[datetime]) -> SessionLifetime:
    """Evaluate the max-age policy for a login session created at ``created_at``."""
    age_limit = max_age()
    if age_limit is None or created_at is None:
        return SessionLifetime(None, False, "disabled")
    mode = legacy_mode()
    start = enforcement_start()
    if start is None:
        if mode == LEGACY_STRICT:
            # Strict does not depend on the instant at all.
            return SessionLifetime(created_at + age_limit, False, "max_age")
        return SessionLifetime(None, False, "unknown")
    legacy = created_at < start
    if not legacy or mode == LEGACY_STRICT:
        return SessionLifetime(created_at + age_limit, legacy, "max_age")
    if mode == LEGACY_FROM_ENFORCEMENT:
        return SessionLifetime(start + age_limit, True, "legacy_from_enforcement")
    return SessionLifetime(None, True, "legacy_exempt")
