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
from datetime import datetime
from typing import List, Optional, Tuple

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import UserApiKey

log = get_logger("Core:ApiKeyService")

# Human-recognisable prefix so leaked keys are easy to identify/grep.
_KEY_PREFIX = "lyk_"


@dataclass
class ResolvedApiKey:
    """Detached snapshot of a verified key (safe to use after the DB session closes)."""

    id: int
    username: str
    label: str
    scopes: List[str] = field(default_factory=list)


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
        effect.  Returns ``None`` otherwise.
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
            resolved = ResolvedApiKey(
                id=int(rec.id),
                username=str(rec.username),
                label=str(rec.label),
                scopes=list(rec.scopes or []),
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_info(rec: UserApiKey) -> ApiKeyInfo:
        return ApiKeyInfo(
            id=int(rec.id),
            label=str(rec.label),
            prefix=str(rec.prefix or ""),
            scopes=list(rec.scopes or []),
            created_at=rec.created_at,
            last_used_at=rec.last_used_at,
            revoked=bool(rec.revoked),
        )


api_key_service = ApiKeyService()
