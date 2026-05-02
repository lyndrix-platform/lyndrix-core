"""
User management service.

Provides CRUD helpers for local user accounts, password changes, and
direct permission grant management.  Password changes are persisted to the
DB immediately; on the next restart the seed logic will overwrite the admin
password if LYNDRIX_ADMIN_PASSWORD differs from the stored hash (env wins).
"""
import os
from typing import List, Optional, Tuple

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import User
from .hashing import hash_password, verify_password

log = get_logger("Core:UserService")

_ADMIN_USER = os.getenv("LYNDRIX_ADMIN_USER", "admin")
_ADMIN_PW   = os.getenv("LYNDRIX_ADMIN_PASSWORD", "lyndrix")


class UserService:
    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_all(self) -> List[User]:
        with db_instance.SessionLocal() as s:
            return s.query(User).order_by(User.username).all()

    def get_by_username(self, username: str) -> Optional[User]:
        with db_instance.SessionLocal() as s:
            return s.query(User).filter(User.username == username).first()

    # ------------------------------------------------------------------
    # Password management
    # ------------------------------------------------------------------

    def change_password(
        self, username: str, current_password: str, new_password: str
    ) -> Tuple[bool, str]:
        """
        Change a user's password after verifying the current one.

        Returns (success, message).
        Note: on next restart, LYNDRIX_ADMIN_PASSWORD in .env will overwrite
        the admin password if it differs from the new hash.
        """
        if len(new_password) < 8:
            return False, "Neues Passwort muss mindestens 8 Zeichen lang sein."

        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user:
                return False, "Benutzer nicht gefunden."
            if not verify_password(str(user.hashed_password), current_password):
                return False, "Aktuelles Passwort ist falsch."
            user.hashed_password = hash_password(new_password)
            s.commit()

        log.info(f"USER: Password changed for '{username}'.")
        return True, "Passwort erfolgreich geändert."

    def admin_reset_password(self, username: str, new_password: str) -> Tuple[bool, str]:
        """Reset a user's password without needing the current one (admin function)."""
        if len(new_password) < 8:
            return False, "Neues Passwort muss mindestens 8 Zeichen lang sein."

        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user:
                return False, "Benutzer nicht gefunden."
            user.hashed_password = hash_password(new_password)
            s.commit()

        log.info(f"USER: Password reset for '{username}' by admin.")
        return True, f"Passwort für '{username}' zurückgesetzt."

    # ------------------------------------------------------------------
    # Extra permissions (direct grants, not via groups)
    # ------------------------------------------------------------------

    def update_extra_permissions(self, username: str, permissions: List[str]) -> bool:
        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user:
                return False
            user.extra_permissions = sorted(set(permissions))
            s.commit()
        log.info(f"USER: Extra permissions updated for '{username}'.")
        return True

    def add_extra_permission(self, username: str, permission: str) -> bool:
        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user:
                return False
            perms = list(user.extra_permissions or [])
            if permission not in perms:
                perms.append(permission)
                user.extra_permissions = sorted(perms)
                s.commit()
        return True

    def remove_extra_permission(self, username: str, permission: str) -> bool:
        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user:
                return False
            perms = list(user.extra_permissions or [])
            if permission in perms:
                perms.remove(permission)
                user.extra_permissions = perms
                s.commit()
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_env_managed(self, username: str) -> bool:
        """True if this account's password is controlled by an env var (admin/bot)."""
        env_user  = os.getenv("LYNDRIX_ADMIN_USER", "admin")
        env_bot   = os.getenv("LYNDRIX_BOT_USER",   "bot")
        return username in (env_user, env_bot)

    def env_override_warning(self, username: str) -> Optional[str]:
        """Return a warning string if the user's password is env-managed."""
        if not self.is_env_managed(username):
            return None
        env_var = "LYNDRIX_ADMIN_PASSWORD" if username == _ADMIN_USER else "LYNDRIX_BOT_PASSWORD"
        return (
            f"Achtung: Beim nächsten Neustart wird das Passwort durch "
            f"{env_var} in der .env überschrieben."
        )


user_service = UserService()
