import os
import logging
import re
from pathlib import Path
from typing import Optional, List, Dict
from pydantic import Field, BaseModel

from pydantic_settings import BaseSettings, SettingsConfigDict

_config_log = logging.getLogger("Core:Config")


class Settings(BaseSettings):
    """
    Central configuration for Lyndrix Core.
    Reads from environment variables, then .env file, then Vault.
    """
    # --- APP INFO ---
    APP_NAME: str = "Lyndrix Core"
    ENV_TYPE: str = Field(default="dev")
    LOG_LEVEL: str = "INFO"
    APP_TITLE: str = "LYNDRIX - DEVELOPER MODE"
    THEME_ENGINE_ENABLED: bool = True
    THEME_DB_OVERRIDES_ENABLED: bool = False
    DEFAULT_THEME_ID: str = "default"
    # --- SERVER ---
    PORT: int = 8081
    STORAGE_SECRET: str = "dev_secret_only"

    # --- PATHS (Cloud Native) ---
    STORAGE_DIR: str = "/data/storage"
    SECURITY_DIR: str = "/data/security"
    LOGS_DIR: str = "/app/logs"
    PLUGINS_DIR: str = "/app/plugins"

    # --- DATABASE ---
    DB_HOST: str = "db"
    DB_USER: str = "admin"
    DB_PASSWORD: str = "secret"
    DB_NAME: str = "lyndrix_db"

    # --- BOOTSTRAP CREDENTIALS ---
    LYNDRIX_ADMIN_USER: str = "admin"
    LYNDRIX_ADMIN_PASSWORD: str = "lyndrix"
    LYNDRIX_ADMIN_EMAIL: str = "admin@lyndrix.local"
    LYNDRIX_BOT_USER: str = "bot"
    LYNDRIX_BOT_PASSWORD: str = "lyndrix-bot"
    # TODO: reject these bootstrap defaults outside dev instead of only warning at runtime.

    # --- VAULT ---
    VAULT_URL: str = "http://vault:8200"
    VAULT_SKIP_VERIFY: bool = True
    LYNDRIX_MASTER_KEY: Optional[str] = None

    # --- CRYPTO & SECURITY ---
    LYNDRIX_ARGON_TIME: int = 3
    LYNDRIX_ARGON_MEM: int = 65536
    LYNDRIX_ARGON_PARALLEL: int = 4

    # --- PLUGIN RECONCILIATION ---
    # Comma-separated list of plugin specs to auto-install on first boot.
    # Format: https://github.com/org/repo[@version]
    LYNDRIX_PLUGINS_DESIRED: Optional[str] = None
    # Whether to auto-update plugins to latest on reboot
    LYNDRIX_PLUGINS_AUTO_UPDATE: bool = False

    # --- AUTH PROVIDERS ---
    # Ordered, comma-separated list of providers to try on login: local, ldap, oidc
    # Custom plugin providers must also be listed here to be registered at startup.
    LYNDRIX_AUTH_PROVIDERS: str = "local"

    # --- SYSTEM API KEY ---
    # Master API key for machine-to-machine access to protected HTTP endpoints.
    # Unset by default: when neither this env var nor the Vault-stored
    # `system_api_key` is configured, the API-key auth method is DISABLED entirely
    # (no implicit/empty key is ever accepted). Can also be set via the Settings UI,
    # which persists it to Vault under lyndrix/core/settings → system_api_key.
    # Resolution order: this env var > Vault > disabled.
    LYNDRIX_SYSTEM_API_KEY: Optional[str] = None

    # --- LDAP ---
    # Full LDAP URL, e.g. ldap://ldap.example.com:389  or  ldaps://…:636
    LYNDRIX_LDAP_URL: Optional[str] = None
    # Service account used to search the directory
    LYNDRIX_LDAP_BIND_DN: Optional[str] = None
    # Stored in Vault under lyndrix/core/auth → ldap_bind_password (env var as fallback)
    LYNDRIX_LDAP_BIND_PASSWORD: Optional[str] = None
    LYNDRIX_LDAP_BASE_DN: Optional[str] = None
    # LDAP search filter — {username} is replaced with the sanitized input
    LYNDRIX_LDAP_USER_FILTER: str = "(uid={username})"
    # Attribute containing group DNs; use 'memberOf' (AD) or 'memberof' (OpenLDAP)
    LYNDRIX_LDAP_GROUP_ATTR: str = "memberOf"
    # JSON mapping from group DN to list of Lyndrix roles
    # e.g. '{"cn=admins,dc=example,dc=com": ["admin", "superadmin"]}'
    LYNDRIX_LDAP_ROLE_MAPPING: Optional[str] = None
    # Roles assigned to every successfully authenticated LDAP user
    LYNDRIX_LDAP_DEFAULT_ROLES: str = "user"
    LYNDRIX_LDAP_TLS_VERIFY: bool = True

    # --- OIDC / OAuth2 (Authentik, Keycloak, Azure AD, …) ---
    # Issuer URL — for Authentik: https://<host>/application/o/<app-slug>
    LYNDRIX_OIDC_ISSUER: Optional[str] = None
    LYNDRIX_OIDC_CLIENT_ID: Optional[str] = None
    # Stored in Vault under lyndrix/core/auth → oidc_client_secret (env var as fallback)
    LYNDRIX_OIDC_CLIENT_SECRET: Optional[str] = None
    # Must match the redirect URI configured in your OIDC provider
    # e.g. https://lyndrix.example.com/auth/callback/oidc
    LYNDRIX_OIDC_REDIRECT_URI: Optional[str] = None
    LYNDRIX_OIDC_SCOPES: str = "openid profile email"
    # Userinfo claim that contains the list of groups/roles
    LYNDRIX_OIDC_ROLE_CLAIM: str = "groups"
    # Comma-separated group names that grant admin + superadmin roles
    LYNDRIX_OIDC_ADMIN_GROUPS: Optional[str] = None
    # Label shown on the SSO login button, e.g. "Authentik" or "Corporate SSO"
    LYNDRIX_OIDC_DISPLAY_NAME: str = "SSO"

    # --- INTERNATIONALISATION ---
    # BCP-47 language tag used when no user preference is stored.
    DEFAULT_LOCALE: str = "en"
    # Comma-separated list of supported locales.  Only locales in this list
    # are offered in the language switcher.
    SUPPORTED_LOCALES: str = "en,de"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

    @property
    def DATABASE_URL(self) -> str:
        return f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}/{self.DB_NAME}"

    @property
    def DATABASE_URL_SAFE(self) -> str:
        """Connection string with credentials redacted for logging."""
        return f"mysql+pymysql://{self.DB_USER}:***@{self.DB_HOST}/{self.DB_NAME}"

    @property
    def LYNDRIX_VAULT_KEY_FILE(self) -> str:
        return f"{self.SECURITY_DIR}/vault_keys.enc"

    @property
    def desired_plugins(self) -> List[str]:
        """Parses LYNDRIX_PLUGINS_DESIRED into a list of plugin URLs."""
        return [plugin["url"] for plugin in self.desired_plugin_specs]

    @property
    def desired_plugin_specs(self) -> List[Dict[str, str]]:
        """Parses desired plugin specs into url/version pairs."""
        if not self.LYNDRIX_PLUGINS_DESIRED:
            return []

        specs = []
        for raw_entry in self.LYNDRIX_PLUGINS_DESIRED.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue

            url = entry
            version = "latest"
            match = re.match(r"^(https?://[^@]+?)(?:@([^/@]+))?$", entry)
            if match:
                url = match.group(1)
                if match.group(2):
                    version = match.group(2)

            specs.append({"url": url, "version": version})

        return specs

    def warn_insecure_defaults(self):
        """Emits log warnings when production-unsafe defaults are active."""
        if self.ENV_TYPE != "dev":
            if self.STORAGE_SECRET == "dev_secret_only":
                _config_log.warning("SECURITY: STORAGE_SECRET is using development default! Set a secure value for production.")
            if self.DB_PASSWORD == "secret":
                _config_log.warning("SECURITY: DB_PASSWORD is using development default! Set a secure value for production.")
            if self.LYNDRIX_ADMIN_PASSWORD == "lyndrix":
                _config_log.warning("SECURITY: LYNDRIX_ADMIN_PASSWORD is using default. Set a secure value for production.")

    # ------------------------------------------------------------------
    # Auth provider helpers
    # ------------------------------------------------------------------

    @property
    def active_auth_providers(self) -> List[str]:
        """Ordered list of provider IDs derived from LYNDRIX_AUTH_PROVIDERS."""
        return [p.strip() for p in self.LYNDRIX_AUTH_PROVIDERS.split(",") if p.strip()]

    @property
    def ldap_role_mapping(self) -> Dict[str, List[str]]:
        """Parse LYNDRIX_LDAP_ROLE_MAPPING JSON into a dict."""
        if not self.LYNDRIX_LDAP_ROLE_MAPPING:
            return {}
        try:
            import json
            return json.loads(self.LYNDRIX_LDAP_ROLE_MAPPING)
        except Exception:
            _config_log.warning("CONFIG: LYNDRIX_LDAP_ROLE_MAPPING is not valid JSON — ignored.")
            return {}

    @property
    def ldap_default_roles(self) -> List[str]:
        return [r.strip() for r in self.LYNDRIX_LDAP_DEFAULT_ROLES.split(",") if r.strip()]

    @property
    def oidc_admin_groups(self) -> List[str]:
        if not self.LYNDRIX_OIDC_ADMIN_GROUPS:
            return []
        return [g.strip() for g in self.LYNDRIX_OIDC_ADMIN_GROUPS.split(",") if g.strip()]

    def get(self, env_var: str, vault_key: str = None, default: str = None) -> str:
        """
        Cloud-Native Configuration Hierarchy (ENV First):
        1. OS Environment Variable
        2. .env File (via Pydantic)
        3. Vault KV (If connected)
        4. Default Value
        """
        # 1. Strict OS Environment check
        val = os.getenv(env_var)
        if val is not None:
            return val

        # 2. Pydantic config (falls back to .env automatically)
        if hasattr(self, env_var):
            val = getattr(self, env_var)
            if val is not None:
                return val

        # 3. Secure Vault integration (Lazy import to avoid circular dependency)
        if vault_key:
            try:
                from core.services import vault_instance
                if vault_instance.is_connected:
                    secret = vault_instance.client.secrets.kv.v2.read_secret_version(path="core/settings", mount_point="lyndrix")
                    if secret and 'data' in secret and 'data' in secret['data']:
                        v_val = secret['data']['data'].get(vault_key)
                        if v_val is not None:
                            return v_val
            except Exception:
                pass

        # 4. Fallback
        if default is None:
            _config_log.warning(f"CONFIG: No value found for '{env_var}' and no default provided.")
        return default


# Singleton for the entire application
settings = Settings()