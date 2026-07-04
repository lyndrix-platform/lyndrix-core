import asyncio
import os
import logging
from sqlalchemy import text
from core.bus import bus
from core.logger import get_logger
from core.components.database.logic.db_service import Base, db_instance
from .hashing import hash_password, verify_password
from .models import User, Group, UserApiKey, UserPreference  # noqa: F401 — imports ensure tables are created

log = get_logger("Core:AuthService")

# Re-export so callers can do: from core.components.auth.logic.auth_service import User
__all__ = ["AuthService", "auth_service", "User", "Group"]

# Environment-backed bootstrap credentials
ADMIN_USERNAME = os.getenv("LYNDRIX_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("LYNDRIX_ADMIN_PASSWORD", "lyndrix")
ADMIN_EMAIL = os.getenv("LYNDRIX_ADMIN_EMAIL", "admin@lyndrix.local")
BOT_USERNAME = os.getenv("LYNDRIX_BOT_USER", "bot")
BOT_PASSWORD = os.getenv("LYNDRIX_BOT_PASSWORD", "lyndrix-bot")

# Warn if insecure defaults are in use
_warn_logger = logging.getLogger("Core:AuthService")


class AuthService:
    def __init__(self):
        bus.subscribe("db:connected")(self.initialize_iam)

    async def initialize_iam(self, payload):
        log.info("IAM: Starting initialization...")
        try:
            # create_all, schema ALTERs, Argon2 seeding and the provider init all
            # do blocking (DB/Vault/CPU) work. This runs as an async bus handler
            # during boot, so offload the whole synchronous block to a worker
            # thread to keep the event loop responsive while the platform comes up.
            await asyncio.to_thread(self._bootstrap_iam)
            log.info("SUCCESS: IAM Service ready.")
            bus.emit("iam:ready")
        except Exception as e:
            log.error(f"ERROR: IAM Service initialization failed: {e}", exc_info=True)

    def _bootstrap_iam(self) -> None:
        """Synchronous IAM bootstrap, executed off the event loop."""
        Base.metadata.create_all(bind=db_instance.engine)
        log.debug("DB: IAM tables checked/created.")
        self._migrate_schema()
        self._seed_users()
        self._initialize_providers()

    def _migrate_schema(self):
        """
        Idempotent column migrations for the users table.
        create_all() never alters existing tables, so we handle new columns here.
        Each ALTER is wrapped individually so a single failure doesn't block boot.
        """
        migrations = [
            # (table, column, column_def)
            ("users", "groups",             "JSON NULL"),
            ("users", "extra_permissions",  "JSON NULL"),
            ("groups", "ldap_mappings",     "JSON NULL"),
        ]
        with db_instance.engine.connect() as conn:
            for table, column, col_def in migrations:
                try:
                    conn.execute(
                        text(
                            f"ALTER TABLE `{table}` ADD COLUMN `{column}` {col_def}"
                        )
                    )
                    conn.commit()
                    log.info(f"MIGRATE: Added column `{table}.{column}`.")
                except Exception as e:
                    err = str(e)
                    if "Duplicate column name" in err or "already exists" in err:
                        pass  # column already present — nothing to do
                    else:
                        log.warning(f"MIGRATE: Could not add `{table}.{column}`: {e}")

    def _initialize_providers(self):
        """Register all configured authentication providers with the provider registry."""
        from .auth_config import auth_config_service, PROVIDER_CHAIN_SPEC
        from .providers.registry import provider_registry
        from .providers.local import LocalProvider
        from .providers.ldap import LDAPProvider
        from .providers.oidc import OIDCProvider

        vault_data = auth_config_service.load_vault_data()
        providers_str, _ = auth_config_service.get_effective(PROVIDER_CHAIN_SPEC, vault_data)
        active = [p.strip() for p in providers_str.split(",") if p.strip()]

        if "local" in active:
            provider_registry.register(LocalProvider())

        if "ldap" in active:
            kwargs = auth_config_service.build_ldap_kwargs(vault_data)
            if kwargs["url"]:
                provider_registry.register(LDAPProvider(**kwargs))
            else:
                log.warning("AUTH: 'ldap' in provider chain but no URL configured — skipped.")

        if "oidc" in active:
            kwargs = auth_config_service.build_oidc_kwargs(vault_data)
            if kwargs["issuer"]:
                provider_registry.register(OIDCProvider(**kwargs))
            else:
                log.warning("AUTH: 'oidc' in provider chain but no issuer configured — skipped.")

        log.info(
            f"AUTH: Provider chain initialized: "
            f"{[p.provider_id for p in provider_registry.get_all()]}"
        )

    def reinitialize_providers(self) -> None:
        """
        Clear and re-register all auth providers from current config.
        Safe to call at runtime from the Settings UI after config changes.
        Any in-progress SSO state is discarded.
        """
        from .providers.registry import provider_registry
        provider_registry.clear()
        self._initialize_providers()
        log.info("AUTH: Providers reinitialized successfully.")

    def _seed_users(self):
        """Seeds admin and bot accounts from environment or defaults.

        Default bootstrap credentials are hard-rejected outside dev mode by
        ``Settings._enforce_secure_defaults`` (config.py), which refuses to start
        the app when LYNDRIX_ADMIN_PASSWORD / LYNDRIX_BOT_PASSWORD still equal
        their shipped defaults. Reaching this seeder with defaults active therefore
        only happens in dev, where the warnings below are sufficient.
        """
        if not db_instance.SessionLocal:
            log.warning("SEED: Aborted, SessionLocal not ready.")
            return

        with db_instance.SessionLocal() as session:
            self._seed_or_update_user(session, ADMIN_USERNAME, ADMIN_PASSWORD, "Lyndrix Administrator", ADMIN_EMAIL, ["admin", "superadmin"])
            self._seed_or_update_user(session, BOT_USERNAME, BOT_PASSWORD, "Lyndrix Bot", f"{BOT_USERNAME}@lyndrix.local", ["bot"])

        # Emit warnings for insecure defaults
        if ADMIN_PASSWORD == "lyndrix":
            log.warning("SECURITY: Admin is using DEFAULT password. Set LYNDRIX_ADMIN_PASSWORD for production.")
        if BOT_PASSWORD == "lyndrix-bot":
            log.warning("SECURITY: Bot is using DEFAULT password. Set LYNDRIX_BOT_PASSWORD for production.")

    def _seed_or_update_user(self, session, username: str, password: str, full_name: str, email: str, roles: list):
        """Creates user if missing. Updates password if environment differs from stored hash."""
        user = session.query(User).filter(User.username == username).first()
        if not user:
            log.info(f"CREATE: Creating user '{username}'...")
            try:
                new_user = User(
                    username=username,
                    full_name=full_name,
                    email=email,
                    hashed_password=hash_password(password),
                    roles=roles
                )
                session.add(new_user)
                session.commit()
                log.info(f"SUCCESS: User '{username}' created.")
            except Exception as e:
                log.error(f"ERROR: User seeding failed for '{username}': {e}", exc_info=True)
                session.rollback()
        else:
            # Update password if env var changed (allows CI/CD credential rotation)
            if not verify_password(str(user.hashed_password), password):
                user.hashed_password = hash_password(password)
                session.commit()
                log.info(f"UPDATE: Password for '{username}' updated from environment.")

    def authenticate_user(self, username: str, password: str):
        """Verifies credentials and returns the User object or None."""
        log.info(f"AUTH: Login attempt for user: {username}")

        if not db_instance.SessionLocal:
            log.error("AUTH: Login impossible: Database session unavailable.")
            return None

        with db_instance.SessionLocal() as session:
            user = session.query(User).filter(User.username == username).first()

            if not user:
                log.warning(f"AUTH: Login failed: User '{username}' does not exist.")
                return None

            if verify_password(str(user.hashed_password), password):
                log.info(f"SUCCESS: Login successful: {username} ({user.full_name})")
                return user

            log.warning(f"AUTH: Login failed: Incorrect password for '{username}'.")
            return None


auth_service = AuthService()