"""
Per-user API key service.

Issues, lists, verifies, and revokes long-lived API keys bound to a specific
local/identity user.  Keys authenticate HTTP requests via the same headers as
the system key (``X-API-Key`` or ``Authorization: Bearer``) but resolve to the
owning user's identity and effective permissions — so they are fully governed by
the Permissions tab (``api:read`` / ``api:write`` …), optionally narrowed by the
key's own ``scopes``.

Only a SHA-256 hash of each key is persisted; the raw key is returned exactly
once at creation time and can never be recovered afterwards.
"""
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from . import session_policy
from .models import User, UserApiKey, UserPreference

log = get_logger("Core:ApiKeyService")

# Human-recognisable prefix so leaked keys are easy to identify/grep.
_KEY_PREFIX = "lyk_"

# Login sessions and user-created API keys live in the same table. A session is
# the row a login minted, recognisable by this label prefix (the React shell's
# "Active sessions" list uses the same rule). The prefix is reserved: the users
# API refuses it for keys a user creates, so a label is a reliable marker.
SESSION_LABEL_PREFIX = "web-session"


def is_session_label(label: Optional[str]) -> bool:
    """True for the label of a login session (as opposed to a named API key)."""
    return str(label or "").strip().lower().startswith(SESSION_LABEL_PREFIX)


def session_max_age() -> Optional[timedelta]:
    """Absolute lifetime of a login session, or ``None`` for "never expires"
    (``LYNDRIX_SESSION_MAX_AGE_HOURS``, 0 = never). Which sessions it applies
    to is decided by ``session_policy`` (sessions from before the rule existed)."""
    return session_policy.max_age()


@dataclass
class ResolvedApiKey:
    """Detached snapshot of a verified key (safe to use after the DB session closes)."""

    id: int
    username: str
    label: str
    scopes: List[str] = field(default_factory=list)
    is_session: bool = False
    # System flags / registry roles and direct grants of the OWNER, read in the
    # same transaction as the key (an identity is never built without an owner).
    owner_roles: List[str] = field(default_factory=list)
    owner_extra_permissions: List[str] = field(default_factory=list)


@dataclass
class ApiKeyInfo:
    """Non-secret metadata for displaying a user's keys."""

    id: int
    label: str
    prefix: str
    scopes: List[str]
    created_at: Optional[datetime]
    last_used_at: Optional[datetime]
    revoked: bool
    # Login sessions only (see session_policy): whether this row is one, when
    # it ends by age (None = it does not) and why — "max_age", "legacy_exempt"
    # (created before the max-age rule applied to this deployment),
    # "legacy_from_enforcement", "disabled" or "unknown".
    is_session: bool = False
    expires_at: Optional[datetime] = None
    expiry: Optional[str] = None


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ApiKeyService:
    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        username: str,
        label: str = "API Key",
        scopes: Optional[List[str]] = None,
    ) -> Tuple[str, ApiKeyInfo]:
        """
        Create a new API key for ``username``.

        Returns ``(raw_key, info)``.  ``raw_key`` is shown to the user once and
        is NOT stored.  ``scopes`` (optional) restricts the key to a subset of
        the owner's permissions; an empty list inherits the owner's full set.
        """
        raw = _KEY_PREFIX + secrets.token_urlsafe(32)
        rec = UserApiKey(
            username=username,
            label=(label or "API Key").strip()[:100],
            prefix=raw[:12],
            hashed_key=_hash_key(raw),
            scopes=sorted(set(scopes or [])),
            revoked=False,
            created_at=datetime.utcnow(),
        )
        with db_instance.SessionLocal() as s:
            s.add(rec)
            s.commit()
            s.refresh(rec)
            info = self._to_info(rec)
        log.info(f"APIKEY: Created key '{info.label}' (id={info.id}) for '{username}'.")
        return raw, info

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_for_user(self, username: str) -> List[ApiKeyInfo]:
        with db_instance.SessionLocal() as s:
            rows = (
                s.query(UserApiKey)
                .filter(UserApiKey.username == username)
                .order_by(UserApiKey.created_at.desc())
                .all()
            )
            return [self._to_info(r) for r in rows]

    def list_all(self) -> List[ApiKeyInfo]:
        with db_instance.SessionLocal() as s:
            rows = s.query(UserApiKey).order_by(UserApiKey.created_at.desc()).all()
            return [self._to_info(r) for r in rows]

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(self, raw_key: str) -> Optional[ResolvedApiKey]:
        """
        Look up a presented raw key.  Returns a detached ``ResolvedApiKey`` when
        the key exists and is not revoked, updating ``last_used_at`` as a side
        effect.  Returns ``None`` otherwise — also when

        * the owner's account no longer exists (a key must die with its user,
          and must not come back to life for a later account of the same name);
        * it is a login session older than ``LYNDRIX_SESSION_MAX_AGE_HOURS``
          (named API keys never expire). The expired row is removed. Sessions
          created before the rule applied to this deployment follow
          ``LYNDRIX_SESSION_MAX_AGE_LEGACY`` — exempt by default — and nothing
          is expired while that cannot be decided (``session_policy``).
        """
        if not raw_key or not raw_key.startswith(_KEY_PREFIX):
            return None
        if db_instance.SessionLocal is None:
            return None

        digest = _hash_key(raw_key)
        with db_instance.SessionLocal() as s:
            rec = (
                s.query(UserApiKey)
                .filter(UserApiKey.hashed_key == digest, UserApiKey.revoked == False)  # noqa: E712
                .first()
            )
            if rec is None:
                return None
            is_session = is_session_label(rec.label)
            if is_session and session_policy.lifetime(rec.created_at).expired():
                try:
                    s.delete(rec)
                    s.commit()
                except Exception:
                    s.rollback()  # a concurrent request removed it first
                return None
            owner = s.query(User).filter(User.username == rec.username).first()
            if owner is None:
                return None
            resolved = ResolvedApiKey(
                id=int(rec.id),
                username=str(owner.username),
                label=str(rec.label),
                scopes=list(rec.scopes or []),
                is_session=is_session,
                owner_roles=list(owner.roles or []),
                owner_extra_permissions=list(owner.extra_permissions or []),
            )
            try:
                rec.last_used_at = datetime.utcnow()
                s.commit()
            except Exception:
                # last_used_at is an audit convenience field — concurrent requests
                # for the same key can produce ER_CHECKREAD (MariaDB 1020) when
                # multiple sessions race to update the same row.  Authentication
                # succeeds regardless.
                s.rollback()
            return resolved

    # ------------------------------------------------------------------
    # Revoke / delete
    # ------------------------------------------------------------------

    def revoke(self, key_id: int, username: Optional[str] = None) -> bool:
        """Revoke a key. If ``username`` is given, only that owner's key is affected."""
        with db_instance.SessionLocal() as s:
            q = s.query(UserApiKey).filter(UserApiKey.id == key_id)
            if username is not None:
                q = q.filter(UserApiKey.username == username)
            rec = q.first()
            if rec is None:
                return False
            rec.revoked = True
            s.commit()
        log.info(f"APIKEY: Revoked key id={key_id}.")
        return True

    def delete(self, key_id: int, username: Optional[str] = None) -> bool:
        with db_instance.SessionLocal() as s:
            q = s.query(UserApiKey).filter(UserApiKey.id == key_id)
            if username is not None:
                q = q.filter(UserApiKey.username == username)
            rec = q.first()
            if rec is None:
                return False
            s.delete(rec)
            s.commit()
        log.info(f"APIKEY: Deleted key id={key_id}.")
        return True

    def revoke_sessions(self, username: str, *, except_key_id: Optional[int] = None) -> int:
        """Delete the login sessions of ``username`` (named API keys stay).

        Called after a password change: whoever held a session with the old
        password is signed out. ``except_key_id`` keeps the session that made
        the change. Returns the number of sessions removed.
        """
        with db_instance.SessionLocal() as s:
            q = s.query(UserApiKey).filter(
                UserApiKey.username == username,
                UserApiKey.label.like(f"{SESSION_LABEL_PREFIX}%"),
            )
            if except_key_id is not None:
                q = q.filter(UserApiKey.id != except_key_id)
            removed = q.delete(synchronize_session=False)
            s.commit()
        if removed:
            log.info(f"APIKEY: Revoked {removed} login session(s) of '{username}'.")
        return int(removed or 0)

    @staticmethod
    def purge_for_username(session, username: str) -> None:
        """Remove keys and preferences stored under ``username`` (caller commits).

        Run in the transaction that DELETES an account, and in the one that
        CREATES one: rows left behind by an earlier account of that name (an
        older core deleted users without their keys) must never be inherited.
        """
        session.query(UserApiKey).filter(UserApiKey.username == username).delete(synchronize_session=False)
        session.query(UserPreference).filter(UserPreference.username == username).delete(synchronize_session=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_info(rec: UserApiKey) -> ApiKeyInfo:
        is_session = is_session_label(rec.label)
        life = session_policy.lifetime(rec.created_at) if is_session else None
        return ApiKeyInfo(
            id=int(rec.id),
            label=str(rec.label),
            prefix=str(rec.prefix or ""),
            scopes=list(rec.scopes or []),
            created_at=rec.created_at,
            last_used_at=rec.last_used_at,
            revoked=bool(rec.revoked),
            is_session=is_session,
            expires_at=life.expires_at if life else None,
            expiry=life.reason if life else None,
        )


api_key_service = ApiKeyService()
