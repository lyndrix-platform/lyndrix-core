import asyncio
from typing import Optional

from core.logger import get_logger
from .base import AuthProvider, AuthResult

log = get_logger("Auth:LocalProvider")


class LocalProvider(AuthProvider):
    """
    Authenticates users against the local Lyndrix database using Argon2 hashes.
    This is always the fallback provider and requires no external infrastructure.

    Throttling is NOT done here: the provider chain runs as one throttled
    attempt in ``provider_registry.authenticate`` (see ``login_guard``).
    """

    provider_id = "local"
    display_name = "Lokal (Datenbank)"

    def is_configured(self) -> bool:
        from core.components.database.logic.db_service import db_instance
        return db_instance.SessionLocal is not None

    def _authenticate_blocking(self, username: str, password: str) -> Optional[AuthResult]:
        """Row lookup + Argon2 comparison (worker thread)."""
        # Local imports to avoid circular dependencies at module load time
        from core.components.database.logic.db_service import db_instance
        from core.components.auth.logic.login_guard import verify_login_password
        from core.components.auth.logic.models import User

        if not db_instance.SessionLocal:
            log.error("AUTH:Local: Database session unavailable.")
            return None

        with db_instance.SessionLocal() as session:
            user = session.query(User).filter(User.username == username).first()

            if not user:
                return None

            # Snapshot the fields we need before leaving the session scope so the
            # (slow) hash verification can run without holding the DB session.
            hashed_password = user.hashed_password
            result = AuthResult(
                username=user.username,
                full_name=user.full_name or username,
                email=user.email or "",
                roles=list(user.roles or []),
                provider=self.provider_id,
            )

        # Bounded: at most LYNDRIX_LOGIN_VERIFY_CONCURRENCY Argon2 runs at a
        # time; raises LoginBusy when no slot frees up (the chain passes it on).
        if verify_login_password(hashed_password, password):
            log.info(f"AUTH:Local: Login successful for '{result.username}'.")
            return result

        log.warning(f"AUTH:Local: Incorrect password for '{result.username}'.")
        return None

    async def authenticate(self, username: str, password: str) -> Optional[AuthResult]:
        # The row lookup is blocking pymysql and Argon2 is deliberately
        # CPU/memory-heavy: both run off the event loop so a login never stalls
        # every other connected client.
        return await asyncio.to_thread(self._authenticate_blocking, username, password)
