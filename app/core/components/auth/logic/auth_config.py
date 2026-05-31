"""
Auth provider configuration service.

Defines all configurable fields for each auth provider and provides
unified read (env → Vault → default) / write (Vault) access.

Priority (highest → lowest):
  1. OS environment variable  — locked in the Settings UI, cannot be overridden
  2. Vault  lyndrix/core/auth  — editable via Settings UI
  3. Built-in default
"""
import os
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger

log = get_logger("Core:AuthConfig")

VAULT_PATH = "core/auth"
VAULT_MOUNT = "lyndrix"


@dataclass
class FieldSpec:
    vault_key: str    # key stored in Vault dict
    env_var: str      # name of the OS environment variable
    default: str      # built-in fallback (always a string)
    label: str        # human-readable label for the Settings UI
    hint: str         # description / example shown below the input
    sensitive: bool = False  # render as password field
    is_bool: bool = False    # render as toggle switch


# ──────────────────────────────────────────────────────────────────────────────
# Field definitions
# ──────────────────────────────────────────────────────────────────────────────

PROVIDER_CHAIN_SPEC = FieldSpec(
    vault_key="auth_providers",
    env_var="LYNDRIX_AUTH_PROVIDERS",
    default="local",
    label="Provider Chain",
    hint="Comma-separated, ordered: local, ldap, oidc  —  custom plugin providers use their provider_id",
)

LDAP_SPECS: List[FieldSpec] = [
    FieldSpec("ldap_url",           "LYNDRIX_LDAP_URL",           "",                  "URL",
              "ldap://host:389  or  ldaps://host:636"),
    FieldSpec("ldap_bind_dn",       "LYNDRIX_LDAP_BIND_DN",       "",                  "Bind DN",
              "Service account DN, e.g. cn=svc-lyndrix,ou=service,dc=example,dc=com"),
    FieldSpec("ldap_bind_password", "LYNDRIX_LDAP_BIND_PASSWORD", "",                  "Bind Password",
              "Service account password — prefer Vault over .env", sensitive=True),
    FieldSpec("ldap_base_dn",       "LYNDRIX_LDAP_BASE_DN",       "",                  "Base DN",
              "Search base, e.g. dc=example,dc=com"),
    FieldSpec("ldap_user_filter",   "LYNDRIX_LDAP_USER_FILTER",   "(uid={username})",  "User Filter",
              "{username} is replaced with the sanitized input. AD: (sAMAccountName={username})"),
    FieldSpec("ldap_group_attr",    "LYNDRIX_LDAP_GROUP_ATTR",    "memberOf",          "Group Attribute",
              "memberOf (Active Directory) / memberof (OpenLDAP)"),
    FieldSpec("ldap_role_mapping",  "LYNDRIX_LDAP_ROLE_MAPPING",  "",                  "Role Mapping",
              'JSON map from group DN to Lyndrix roles, e.g. {"cn=admins,dc=example,dc=com": ["admin","superadmin"]}'),
    FieldSpec("ldap_default_roles", "LYNDRIX_LDAP_DEFAULT_ROLES", "user",              "Default Roles",
              "Roles assigned to every successfully authenticated LDAP user (comma-separated)"),
    FieldSpec("ldap_tls_verify",    "LYNDRIX_LDAP_TLS_VERIFY",    "true",              "TLS Verify",
              "Verify the server TLS certificate (true / false)", is_bool=True),
]

OIDC_SPECS: List[FieldSpec] = [
    FieldSpec("oidc_issuer",        "LYNDRIX_OIDC_ISSUER",        "",                       "Issuer URL",
              "Authentik: https://<host>/application/o/<app-slug>  |  Keycloak: https://<host>/realms/<realm>"),
    FieldSpec("oidc_client_id",     "LYNDRIX_OIDC_CLIENT_ID",     "",                       "Client ID",
              "OAuth2 Client ID from your OIDC provider"),
    FieldSpec("oidc_client_secret", "LYNDRIX_OIDC_CLIENT_SECRET", "",                       "Client Secret",
              "OAuth2 Client Secret — strongly prefer Vault over .env for this value", sensitive=True),
    FieldSpec("oidc_redirect_uri",  "LYNDRIX_OIDC_REDIRECT_URI",  "",                       "Redirect URI",
              "https://<lyndrix-host>/auth/callback/oidc  — must match exactly what is registered in the provider"),
    FieldSpec("oidc_scopes",        "LYNDRIX_OIDC_SCOPES",        "openid profile email",   "Scopes",
              "Space-separated OIDC scopes"),
    FieldSpec("oidc_role_claim",    "LYNDRIX_OIDC_ROLE_CLAIM",    "groups",                 "Role Claim",
              "Name of the userinfo claim that contains the list of groups or roles"),
    FieldSpec("oidc_admin_groups",  "LYNDRIX_OIDC_ADMIN_GROUPS",  "",                       "Admin Groups",
              "Comma-separated group names that grant admin + superadmin roles"),
    FieldSpec("oidc_display_name",  "LYNDRIX_OIDC_DISPLAY_NAME",  "SSO",                    "Button Label",
              'Text shown on the SSO login button, e.g. "Authentik" or "Corporate SSO"'),
]

ALL_SPECS: List[FieldSpec] = [PROVIDER_CHAIN_SPEC] + LDAP_SPECS + OIDC_SPECS
_SPEC_MAP: Dict[str, FieldSpec] = {s.vault_key: s for s in ALL_SPECS}


# ──────────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────────

class AuthConfigService:
    """
    Reads and writes auth provider configuration.

    Effective value resolution order (highest priority first):
      1. OS environment variable  → locked in UI, takes precedence always
      2. Vault  (core/auth)       → editable via Settings UI
      3. Built-in default         → shown when nothing is configured
    """

    # ------------------------------------------------------------------
    # Vault I/O
    # ------------------------------------------------------------------

    def _vault_client(self):
        from core.services import vault_instance
        return vault_instance

    def load_vault_data(self) -> dict:
        vault = self._vault_client()
        if not vault.is_connected:
            return {}
        try:
            resp = vault.client.secrets.kv.v2.read_secret_version(
                path=VAULT_PATH, mount_point=VAULT_MOUNT
            )
            return resp["data"]["data"] or {}
        except Exception:
            return {}

    def save_vault_data(self, updates: dict) -> None:
        """Merge `updates` into the existing Vault secret at core/auth."""
        vault = self._vault_client()
        if not vault.is_connected:
            raise RuntimeError("Vault ist nicht verbunden. Konfiguration kann nicht gespeichert werden.")
        existing = self.load_vault_data()
        existing.update({k: v for k, v in updates.items() if v != ""})
        # Remove keys explicitly set to "" to allow clearing values
        for k, v in updates.items():
            if v == "":
                existing.pop(k, None)
        vault.client.secrets.kv.v2.create_or_update_secret(
            path=VAULT_PATH, mount_point=VAULT_MOUNT, secret=existing
        )

    # ------------------------------------------------------------------
    # Effective value resolution
    # ------------------------------------------------------------------

    def get_effective(self, spec: FieldSpec, vault_data: dict) -> Tuple[str, str]:
        """
        Returns (effective_value, source).
        source ∈ { 'env', 'vault', 'default' }
        """
        env_val = os.environ.get(spec.env_var)
        if env_val is not None:
            return env_val, "env"
        vault_val = vault_data.get(spec.vault_key)
        if vault_val is not None:
            return str(vault_val), "vault"
        return spec.default, "default"

    def get_effective_str(self, vault_key: str, vault_data: dict) -> str:
        """Returns just the effective value string for the given vault_key."""
        spec = _SPEC_MAP.get(vault_key)
        if not spec:
            return ""
        value, _ = self.get_effective(spec, vault_data)
        return value

    def get_all_effective(self, vault_data: Optional[dict] = None) -> Dict[str, Tuple[str, str]]:
        """Returns {vault_key: (value, source)} for all known fields."""
        if vault_data is None:
            vault_data = self.load_vault_data()
        return {spec.vault_key: self.get_effective(spec, vault_data) for spec in ALL_SPECS}

    # ------------------------------------------------------------------
    # Provider kwargs builders (used by auth_service)
    # ------------------------------------------------------------------

    def build_ldap_kwargs(self, vault_data: dict) -> dict:
        def eff(key): return self.get_effective_str(key, vault_data)

        role_mapping: dict = {}
        raw = eff("ldap_role_mapping")
        if raw:
            try:
                role_mapping = json.loads(raw)
            except Exception:
                log.warning("AUTH: ldap_role_mapping is not valid JSON — ignored.")

        default_roles = [r.strip() for r in eff("ldap_default_roles").split(",") if r.strip()] or ["user"]
        tls_verify = eff("ldap_tls_verify").lower() not in ("false", "0", "no")

        return dict(
            url=eff("ldap_url"),
            bind_dn=eff("ldap_bind_dn"),
            bind_password=eff("ldap_bind_password"),
            base_dn=eff("ldap_base_dn"),
            user_filter=eff("ldap_user_filter") or "(uid={username})",
            group_attr=eff("ldap_group_attr") or "memberOf",
            role_mapping=role_mapping,
            default_roles=default_roles,
            tls_verify=tls_verify,
        )

    def build_oidc_kwargs(self, vault_data: dict) -> dict:
        def eff(key): return self.get_effective_str(key, vault_data)

        admin_groups = [g.strip() for g in eff("oidc_admin_groups").split(",") if g.strip()]
        return dict(
            issuer=eff("oidc_issuer"),
            client_id=eff("oidc_client_id"),
            client_secret=eff("oidc_client_secret"),
            redirect_uri=eff("oidc_redirect_uri"),
            scopes=eff("oidc_scopes") or "openid profile email",
            role_claim=eff("oidc_role_claim") or "groups",
            admin_groups=admin_groups,
            display_name_override=eff("oidc_display_name"),
        )


auth_config_service = AuthConfigService()
