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
# Break-glass: additionally seed the superadmin system flag onto the admin
# account. Default OFF — INT_ADMIN group membership is the normal admin path.
ADMIN_FORCE_SUPERADMIN = os.getenv("LYNDRIX_ADMIN_FORCE_SUPERADMIN", "").strip().lower() in ("1", "true", "yes")

# Default groups seeded at IAM bootstrap (Identity & Permissions 2.0).
# create-if-missing by name — an admin's later edits are NEVER overwritten.
_DEFAULT_GROUPS: list[tuple[str, str, list]] = [
    (
        "INT_VIEWER",
        "Built-in viewer group: read-only platform access.",
        ["feature:dashboard.view", "api:read"],
    ),
    (
        "INT_USER",
        "Built-in user group: read/write platform access, no administration.",
        ["feature:dashboard.view", "feature:dashboard.admin_sections", "api:read", "api:write"],
    ),
    (
        "INT_ADMIN",
        "Built-in administrator group: every core feature gate + full API access.",
        [
            "feature:dashboard.view", "feature:dashboard.admin_sections",
            "feature:settings.view", "feature:settings.edit",
            "feature:users.view", "feature:users.edit",
            "feature:groups.view", "feature:groups.edit",
            "feature:auth.configure", "feature:plugins.manage",
            "api:read", "api:write", "admin:read", "admin:write",
        ],
    ),
]

# Warn if insecure defaults are in use
_warn_logger = logging.getLogger("Core:AuthService")


class AuthService:
    def __init__(self):
        bus.subscribe("db:connected")(self.initialize_iam)
        # Manifests are only fully loaded after boot — that's when per-plugin
        # baseline grants + manifest-role auto-mappings can be applied. A
        # freshly installed/enabled plugin re-triggers the (ledger-idempotent)
        # sync so its baseline grants + declared roles land immediately.
        bus.subscribe("system:boot_complete")(self._sync_roles_on_boot)
        bus.subscribe("plugin:state_changed")(self._sync_roles_on_boot)

    async def _sync_roles_on_boot(self, payload=None):
        try:
            from .role_registry import role_registry

            # Sync does blocking DB work — keep it off the shared event loop.
            await asyncio.to_thread(role_registry.sync)
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"IAM: role sync after boot failed: {e}")

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
        self._seed_default_groups()
        self._ensure_admin_permissions()
        self._seed_users()
        self._initialize_providers()

    def _seed_default_groups(self) -> None:
        """Create the built-in INT_* groups if missing (Identity 2.0).

        create-if-missing ONLY: a group that already exists is left untouched,
        so an admin's permission edits survive every restart. Per-plugin
        baseline grants + manifest-role auto-mapping happen later (on
        system:boot_complete, once manifests are loaded) via role_registry.
        """
        if not db_instance.SessionLocal:
            return
        with db_instance.SessionLocal() as session:
            for name, description, permissions in _DEFAULT_GROUPS:
                if session.query(Group).filter(Group.name == name).first() is None:
                    session.add(Group(
                        name=name,
                        description=description,
                        permissions=list(permissions),
                        ldap_mappings=[],
                    ))
                    log.info(f"SEED: Created default group '{name}'.")
            session.commit()

    def _ensure_admin_permissions(self) -> None:
        """One-time additive migration: guarantee INT_ADMIN carries admin:read/admin:write.

        ``_seed_default_groups`` is create-if-missing only, so a pre-existing
        INT_ADMIN row (seeded before the admin:* tier existed) never picks up
        the new permissions on its own. This ADDS only the missing admin:*
        entries to that single row and leaves everything else — including any
        permission an operator removed on purpose — untouched. Safe to run on
        every boot; a no-op once the entries are present.
        """
        if not db_instance.SessionLocal:
            return
        with db_instance.SessionLocal() as session:
            grp = session.query(Group).filter(Group.name == "INT_ADMIN").first()
            if grp is None:
                return  # not seeded yet this pass — the next boot picks it up
            current = list(grp.permissions or [])
            missing = [p for p in ("admin:read", "admin:write") if p not in current]
            if missing:
                grp.permissions = sorted(current + missing)
                session.commit()
                log.info(f"MIGRATE: Added {missing} to 'INT_ADMIN' group permissions.")

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
            ("groups", "roles",             "JSON NULL"),
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

        # Identity 2.0: admin is an ordinary INT_ADMIN member exercised through
        # the normal group-resolution path; the superadmin system flag is a
        # break-glass opt-in (LYNDRIX_ADMIN_FORCE_SUPERADMIN=true).
        admin_roles = ["superadmin"] if ADMIN_FORCE_SUPERADMIN else []
        with db_instance.SessionLocal() as session:
            self._seed_or_update_user(session, ADMIN_USERNAME, ADMIN_PASSWORD, "Lyndrix Administrator", ADMIN_EMAIL, admin_roles, groups=["INT_ADMIN"])
            self._seed_or_update_user(session, BOT_USERNAME, BOT_PASSWORD, "Lyndrix Bot", f"{BOT_USERNAME}@lyndrix.local", ["bot"], groups=["INT_USER"])

        # Emit warnings for insecure defaults
        if ADMIN_PASSWORD == "lyndrix":
            log.warning("SECURITY: Admin is using DEFAULT password. Set LYNDRIX_ADMIN_PASSWORD for production.")
        if BOT_PASSWORD == "lyndrix-bot":
            log.warning("SECURITY: Bot is using DEFAULT password. Set LYNDRIX_BOT_PASSWORD for production.")

    def _seed_or_update_user(self, session, username: str, password: str, full_name: str, email: str, roles: list, groups: list | None = None):
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
                    roles=roles,
                    groups=list(groups or []),
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
            # Backfill default group membership for pre-2.0 seeded accounts so
            # a non-reset dev DB still lands in the group path (never removes
            # anything an admin assigned).
            if groups:
                current = list(user.groups or [])
                missing = [g for g in groups if g not in current]
                if missing:
                    user.groups = sorted(current + missing)
                    session.commit()
                    log.info(f"UPDATE: Added '{username}' to default group(s) {missing}.")

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