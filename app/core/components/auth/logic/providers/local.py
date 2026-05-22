from typing import Optional

from core.logger import get_logger
from .base import AuthProvider, AuthResult

log = get_logger("Auth:LocalProvider")


class LocalProvider(AuthProvider):
    """
    Authenticates users against the local Lyndrix database using Argon2 hashes.
    This is always the fallback provider and requires no external infrastructure.
    """

    provider_id = "local"
    display_name = "Lokal (Datenbank)"

    def is_configured(self) -> bool:
        from core.components.database.logic.db_service import db_instance
        return db_instance.SessionLocal is not None

    async def authenticate(self, username: str, password: str) -> Optional[AuthResult]:
        # Local imports to avoid circular dependencies at module load time
        from core.components.database.logic.db_service import db_instance
        from core.components.auth.logic.models import User
        from core.components.auth.logic.hashing import verify_password

        # TODO: add throttling / backoff for repeated login attempts before calling the password verifier.

        if not db_instance.SessionLocal:
            log.error("AUTH:Local: Database session unavailable.")
            return None

        with db_instance.SessionLocal() as session:
            user = session.query(User).filter(User.username == username).first()

            if not user:
                return None

            if verify_password(str(user.hashed_password), password):
                log.info(f"AUTH:Local: Login successful for '{username}'.")
                return AuthResult(
                    username=user.username,
                    full_name=user.full_name or username,
                    email=user.email or "",
                    roles=list(user.roles or []),
                    provider=self.provider_id,
                )

        log.warning(f"AUTH:Local: Incorrect password for '{username}'.")
        return None
