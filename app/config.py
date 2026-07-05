import os
import sys
import logging
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, List, Dict, Optional
from pydantic import Field, ValidationError, model_validator

from pydantic_settings import BaseSettings, SettingsConfigDict

_config_log = logging.getLogger("Core:Config")

# Fields whose shipped default is a development-only placeholder. Using any of
# these in a non-dev deployment is rejected at startup (see _enforce_secure_defaults).
_INSECURE_DEFAULTS: Dict[str, str] = {
    "STORAGE_SECRET": "dev_secret_only",
    "DB_PASSWORD": "secret",
    "LYNDRIX_ADMIN_PASSWORD": "lyndrix",
    "LYNDRIX_BOT_PASSWORD": "lyndrix-bot",
}


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
    # Insecure defaults above are hard-rejected outside dev — see _enforce_secure_defaults().

    # --- VAULT ---
    VAULT_URL: str = "http://vault:8200"
    # Secure by default: Vault TLS verification is enabled. Only disable in dev
    # against a self-signed Vault (non-dev + skip-verify is rejected at startup).
    VAULT_SKIP_VERIFY: bool = False
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
    # Git URL of the plugin collection/registry used by the marketplace.
    LYNDRIX_PLUGIN_COLLECTION_URL: str = (
        "https://github.com/lyndrix-platform/lyndrix-plugin-collection.git"
    )
    # Interval for the background plugin-update check (seconds; min 300).
    LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S: int = 21600
    # Comma-separated allowlist of git hostnames the plugin installer may talk
    # to (install/upgrade/version-fetch). Caller-supplied repo URLs (and any
    # PAT configured for a non-GitHub host) are rejected outside this list —
    # the SSRF guard in core.components.plugins.logic.git_providers. Add your
    # self-hosted GitLab's hostname here to install plugins from it.
    LYNDRIX_PLUGIN_REPO_HOSTS: str = "github.com"

    # --- AUTH PROVIDERS ---
    # Ordered, comma-separated list of providers to try on login: local, ldap, oidc
    # Custom plugin providers must also be listed here to be registered at startup.
    LYNDRIX_AUTH_PROVIDERS: str = "local"

    # --- CORS ---
    # Origins allowed for cross-origin requests (lyndrix-ui dev server and custom frontends).
    CORS_ALLOWED_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:4321"]

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
    DEFAULT_LOCALE: str = "de"
    # Comma-separated list of supported locales.  Only locales in this list
    # are offered in the language switcher.
    SUPPORTED_LOCALES: str = "en,de"

    # --- MESSAGING GATEWAY ---
    # Comma-separated list of provider instances in the format "type:instance_id".
    # Multiple instances of the same type are supported (e.g. two Discord webhooks).
    # Example: "discord:ops,discord:alerts,slack:team"
    # Per-instance config is read from env vars: LYNDRIX_GATEWAY_{TYPE}_{INSTANCE_ID}_{SETTING}
    LYNDRIX_GATEWAY_PROVIDERS: str = ""
    # Maximum seconds to wait for a single adapter.send() call before timing out.
    # Adapters can override this per-class via send_timeout: ClassVar[float].
    LYNDRIX_GATEWAY_SEND_TIMEOUT_SECONDS: float = 30.0
    # Maximum number of retry attempts for a failed dispatch (background retry worker).
    LYNDRIX_GATEWAY_MAX_RETRY_ATTEMPTS: int = 3
    # TTL in seconds for pending correlation actions (interactive button responses).
    LYNDRIX_GATEWAY_CORRELATION_TTL_SECONDS: int = 3600

    # --- UI ENGINE ---
    # Comma-separated list of UI engines to activate alongside the API:
    #   "api"     — pure API only, no UI (default)
    #   "react"   — React SPA served at /app (requires app/ui_dist build)
    #   "nicegui" — NiceGUI admin dashboard at / (optional, session-based)
    # Examples: "react", "nicegui", "react,nicegui"
    # The API always runs regardless of this setting.
    LYNDRIX_UI_ENGINE: str = "api"

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

    @property
    def gateway_provider_specs(self) -> List[Dict[str, str]]:
        """Parse LYNDRIX_GATEWAY_PROVIDERS into structured provider configs.

        Each entry contains::

            {"type": "discord", "instance_id": "ops",
             "env_prefix": "LYNDRIX_GATEWAY_DISCORD_OPS"}

        Returns an empty list when LYNDRIX_GATEWAY_PROVIDERS is unset.
        """
        if not self.LYNDRIX_GATEWAY_PROVIDERS:
            return []
        specs: List[Dict[str, str]] = []
        for raw in self.LYNDRIX_GATEWAY_PROVIDERS.split(","):
            entry = raw.strip()
            if not entry:
                continue
            if ":" not in entry:
                import logging as _logging
                _logging.getLogger("Core:Config").warning(
                    "LYNDRIX_GATEWAY_PROVIDERS: ignoring malformed entry '%s' "
                    "(expected format 'type:instance_id', e.g. 'discord:ops')",
                    entry,
                )
                continue
            ptype, _, instance_id = entry.partition(":")
            ptype       = ptype.strip()
            instance_id = instance_id.strip()
            prefix = (
                f"LYNDRIX_GATEWAY_{ptype.upper()}_"
                f"{instance_id.upper().replace('-', '_').replace(' ', '_')}"
            )
            specs.append({"type": ptype, "instance_id": instance_id, "env_prefix": prefix})
        return specs

    def get_gateway_provider_setting(self, env_prefix: str, key: str) -> Optional[str]:
        """Return the env var value for a per-instance provider setting.

        Looks up ``{env_prefix}_{key.upper()}`` from the OS environment.
        Returns ``None`` when the variable is not set; callers should fall back
        to Vault via ``ctx.get_secret()`` in that case.

        Example::

            # LYNDRIX_GATEWAY_DISCORD_OPS_WEBHOOK_URL=https://…
            val = settings.get_gateway_provider_setting(
                "LYNDRIX_GATEWAY_DISCORD_OPS", "WEBHOOK_URL"
            )
        """
        full_key = f"{env_prefix}_{key.upper().replace('-', '_')}"
        return os.getenv(full_key)

    def _insecure_default_violations(self) -> List[str]:
        """List the production-unsafe defaults that are currently active."""
        violations = [
            name
            for name, dev_default in _INSECURE_DEFAULTS.items()
            if getattr(self, name) == dev_default
        ]
        if self.VAULT_SKIP_VERIFY:
            violations.append("VAULT_SKIP_VERIFY (Vault TLS verification disabled)")
        return violations

    @model_validator(mode="after")
    def _enforce_secure_defaults(self) -> "Settings":
        """Fail fast in non-dev when production-unsafe defaults are still active.

        In dev these are only logged as warnings; in any other environment the
        application refuses to start so a misconfigured prod instance never boots
        with default credentials, a guessable session secret, or Vault TLS off.
        """
        if self.ENV_TYPE == "dev":
            self.warn_insecure_defaults()
            return self
        violations = self._insecure_default_violations()
        if violations:
            bullet_list = "".join(f"\n    {v}" for v in violations)
            raise ValueError(
                f"Insecure development defaults detected (ENV_TYPE={self.ENV_TYPE!r}).\n"
                f"Set these environment variables to secure values before deploying:{bullet_list}"
            )
        return self

    def warn_insecure_defaults(self) -> None:
        """Emit a log warning for every production-unsafe default in use."""
        for violation in self._insecure_default_violations():
            _config_log.warning(
                "SECURITY: insecure development default active — %s. "
                "Set a secure value for production.",
                violation,
            )

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
        except (json.JSONDecodeError, TypeError):
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

    def hydrate_from_vault(self) -> int:
        """
        Apply Vault-stored values (lyndrix/core/settings) onto editable fields.

        Environment variables always win: a field whose OS environment variable
        is set is never overridden here.  This is what makes UI-saved settings
        survive a reboot while keeping env vars authoritative.

        Returns the number of fields that were applied.
        """
        try:
            from core.services import vault_instance
        except ImportError:
            return 0
        if not getattr(vault_instance, "is_connected", False):
            return 0
        try:
            resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                path="core/settings", mount_point="lyndrix"
            )
            data = resp["data"]["data"] or {}
        except Exception as exc:
            _config_log.warning("CONFIG: Could not read settings from Vault — skipped: %s", exc)
            return 0

        applied = 0
        for spec in EDITABLE_SETTINGS:
            if spec.field not in data:
                continue
            if os.getenv(spec.field) is not None:
                continue  # env var takes precedence — never override
            try:
                setattr(self, spec.field, spec.coerce(data[spec.field]))
                applied += 1
            except Exception:
                _config_log.warning(
                    f"CONFIG: Could not apply Vault value for '{spec.field}' — skipped."
                )
        if applied:
            _config_log.info(f"CONFIG: Hydrated {applied} setting(s) from Vault.")
        return applied

    def get(self, env_var: str, vault_key: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
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
            except Exception as exc:
                _config_log.warning("CONFIG: Vault lookup for '%s' failed: %s", vault_key, exc)

        # 4. Fallback
        if default is None:
            _config_log.warning(f"CONFIG: No value found for '{env_var}' and no default provided.")
        return default


# Singleton for the entire application
try:
    settings = Settings()
except ValidationError as _exc:
    _lines = ["", "=" * 68, "FATAL: Lyndrix refused to start — configuration error", "=" * 68]
    for _err in _exc.errors():
        _msg = _err.get("msg", "")
        _msg = _msg.removeprefix("Value error, ")
        _lines.append(_msg)
    _lines.append("=" * 68)
    print("\n".join(_lines), file=sys.stderr)
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Editable settings registry (System tab in the Settings UI)
#
# These are the runtime-relevant application settings that may be changed from
# the dashboard.  Each entry's `field` is BOTH the Settings attribute and the OS
# environment variable name (pydantic-settings reads env vars matching the field
# name).  Values saved from the UI are persisted to Vault (lyndrix/core/settings)
# and re-applied on boot via Settings.hydrate_from_vault(), unless the matching
# environment variable is set — in which case the env var always wins.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class EditableSetting:
    field: str                 # Settings attribute == OS env var name
    label: str                 # human-friendly label
    description: str           # shown in the UI (env var key is appended automatically)
    kind: str = "str"          # "str" | "bool" | "int" | "select"
    options: List[str] = dataclass_field(default_factory=list)  # for kind == "select"
    category: str = "Application"
    sensitive: bool = False

    @property
    def env_var(self) -> str:
        return self.field

    def coerce(self, raw: Any) -> Any:
        """Convert a raw (UI / Vault) value into the proper Python type."""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if self.kind == "int":
            return int(raw)
        return "" if raw is None else str(raw)


EDITABLE_SETTINGS: List[EditableSetting] = [
    # ── Application ──────────────────────────────────────────────────────────
    EditableSetting("APP_NAME", "Application Name",
                    "Internal application name used across the platform.",
                    category="Application"),
    EditableSetting("APP_TITLE", "Application Title",
                    "Title shown in the browser tab and UI header.",
                    category="Application"),
    EditableSetting("LOG_LEVEL", "Log Level",
                    "Root logging verbosity. Applied immediately on save.",
                    kind="select",
                    options=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                    category="Application"),
    EditableSetting("LYNDRIX_UI_ENGINE", "UI Engine",
                    "Which UI engine core serves. 'react'/'both' require the built "
                    "React bundle (app/ui_dist). Takes effect on restart.",
                    kind="select",
                    options=["nicegui", "react", "both"],
                    category="Application"),
    # ── Localization ─────────────────────────────────────────────────────────
    EditableSetting("DEFAULT_LOCALE", "Default Locale",
                    "BCP-47 language tag used when a user has no preference (e.g. en, de).",
                    category="Localization"),
    EditableSetting("SUPPORTED_LOCALES", "Supported Locales",
                    "Comma-separated list of locales offered in the language switcher.",
                    category="Localization"),
    # ── Theming ──────────────────────────────────────────────────────────────
    EditableSetting("THEME_ENGINE_ENABLED", "Theme Engine Enabled",
                    "Enable the dynamic theme engine.",
                    kind="bool", category="Theming"),
    EditableSetting("THEME_DB_OVERRIDES_ENABLED", "Theme DB Overrides Enabled",
                    "Allow theme overrides stored in the database to take effect.",
                    kind="bool", category="Theming"),
    EditableSetting("DEFAULT_THEME_ID", "Default Theme ID",
                    "Identifier of the theme applied by default.",
                    category="Theming"),
    # ── Plugins ──────────────────────────────────────────────────────────────
    EditableSetting("LYNDRIX_PLUGINS_DESIRED", "Desired Plugins",
                    "Comma-separated plugin specs to reconcile on boot: "
                    "https://github.com/org/repo[@version].",
                    category="Plugins"),
    EditableSetting("LYNDRIX_PLUGINS_AUTO_UPDATE", "Auto-Update Plugins On Boot",
                    "Automatically update installed plugins to the latest version on reboot.",
                    kind="bool", category="Plugins"),
    EditableSetting("LYNDRIX_PLUGIN_COLLECTION_URL", "Plugin Collection URL",
                    "Git URL of the plugin collection/registry used by the marketplace.",
                    category="Plugins"),
    EditableSetting("LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S", "Update Check Interval (s)",
                    "How often the background job checks installed plugins for newer "
                    "release tags (seconds, minimum 300).",
                    kind="int", category="Plugins"),
    EditableSetting("LYNDRIX_PLUGIN_REPO_HOSTS", "Allowed Plugin Repo Hosts",
                    "Comma-separated git hostnames the plugin installer may fetch "
                    "from (install/upgrade/version-fetch). Add a self-hosted GitLab "
                    "hostname here to install plugins from it.",
                    category="Plugins"),
]

EDITABLE_SETTING_CATEGORIES: List[str] = ["Application", "Localization", "Theming", "Plugins"]