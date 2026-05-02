from typing import Optional, List, Dict

from core.logger import get_logger
from .base import AuthProvider, AuthResult

log = get_logger("Auth:LDAPProvider")


class LDAPProvider(AuthProvider):
    """
    Authenticates users against an LDAP / Active Directory server.

    Flow:
      1. Bind with the service account (bind_dn / bind_password) to locate the user DN.
      2. Re-bind as the user with the supplied password to verify credentials.
      3. Read group memberships and map them to Lyndrix roles via role_mapping.

    Required env vars (set via LYNDRIX_LDAP_* or via the Settings UI → Vault):
      LYNDRIX_LDAP_URL          e.g. ldap://ldap.example.com:389 or ldaps://…:636
      LYNDRIX_LDAP_BIND_DN      e.g. cn=svc-lyndrix,ou=service,dc=example,dc=com
      LYNDRIX_LDAP_BIND_PASSWORD
      LYNDRIX_LDAP_BASE_DN      e.g. dc=example,dc=com

    Optional env vars:
      LYNDRIX_LDAP_USER_FILTER   default: (uid={username})  — use sAMAccountName for AD
      LYNDRIX_LDAP_GROUP_ATTR    default: memberOf
      LYNDRIX_LDAP_ROLE_MAPPING  JSON map: {"cn=admins,...": ["admin","superadmin"]}
      LYNDRIX_LDAP_DEFAULT_ROLES default: user
      LYNDRIX_LDAP_TLS_VERIFY    default: true
    """

    provider_id = "ldap"
    display_name = "LDAP / Active Directory"

    def __init__(
        self,
        url: str,
        bind_dn: str,
        bind_password: str,
        base_dn: str,
        user_filter: str = "(uid={username})",
        group_attr: str = "memberOf",
        role_mapping: Optional[Dict[str, List[str]]] = None,
        default_roles: Optional[List[str]] = None,
        tls_verify: bool = True,
    ):
        self.url = url
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.base_dn = base_dn
        self.user_filter = user_filter
        self.group_attr = group_attr
        self.role_mapping = role_mapping or {}
        self.default_roles = default_roles or ["user"]
        self.tls_verify = tls_verify

    def is_configured(self) -> bool:
        return bool(self.url and self.bind_dn and self.bind_password and self.base_dn)

    def _map_groups_to_roles(self, groups: List[str]) -> List[str]:
        roles: set = set(self.default_roles)
        for group in groups:
            mapped = self.role_mapping.get(group, [])
            roles.update(mapped)
        return sorted(roles)

    async def authenticate(self, username: str, password: str) -> Optional[AuthResult]:
        try:
            import ldap3
            import ldap3.utils.conv
        except ImportError:
            log.error("AUTH:LDAP: ldap3 package not available.")
            return None

        if not self.is_configured():
            log.warning("AUTH:LDAP: Provider not fully configured.")
            return None

        # Prevent LDAP injection in the search filter
        safe_username = ldap3.utils.conv.escape_filter_chars(username)
        user_filter = self.user_filter.replace("{username}", safe_username)

        use_ssl = self.url.lower().startswith("ldaps://")
        tls = ldap3.Tls(validate=2 if self.tls_verify else 0)
        server = ldap3.Server(self.url, get_info=ldap3.ALL, use_ssl=use_ssl, tls=tls)

        try:
            # Step 1: service-account bind to locate user DN
            svc_conn = ldap3.Connection(
                server,
                user=self.bind_dn,
                password=self.bind_password,
                auto_bind=True,
            )
            svc_conn.search(
                self.base_dn,
                user_filter,
                attributes=[
                    self.group_attr,
                    "cn",
                    "displayName",
                    "mail",
                    "sAMAccountName",
                    "uid",
                ],
            )
            if not svc_conn.entries:
                log.warning(f"AUTH:LDAP: User '{username}' not found in directory.")
                svc_conn.unbind()
                return None

            entry = svc_conn.entries[0]
            user_dn = entry.entry_dn
            svc_conn.unbind()

            # Step 2: bind as the user to verify the password
            user_conn = ldap3.Connection(server, user=user_dn, password=password)
            if not user_conn.bind():
                log.warning(f"AUTH:LDAP: Incorrect password for '{username}'.")
                return None
            user_conn.unbind()

        except ldap3.core.exceptions.LDAPException as e:
            log.error(f"AUTH:LDAP: Connection/search error: {e}", exc_info=True)
            return None

        # Step 3: build AuthResult from directory attributes
        display_name_val = (
            entry["displayName"].value if "displayName" in entry.entry_attributes else None
        )
        cn_val = entry["cn"].value if "cn" in entry.entry_attributes else None
        full_name = str(display_name_val or cn_val or username)
        email_val = entry["mail"].value if "mail" in entry.entry_attributes else None
        email = str(email_val) if email_val else f"{username}@ldap.local"

        raw_groups = (
            list(entry[self.group_attr].values)
            if self.group_attr in entry.entry_attributes
            else []
        )
        groups = [str(g) for g in raw_groups]
        roles = self._map_groups_to_roles(groups)

        # Also resolve local group names from LDAP DNs so permissions defined
        # in the Groups UI are automatically applied on LDAP login.
        try:
            from core.components.auth.logic.group_service import group_service
            local_group_names = group_service.resolve_ldap_groups(groups)
            if local_group_names:
                roles = sorted(set(roles) | set(local_group_names))
        except Exception as exc:
            log.warning(f"AUTH:LDAP: local group resolution failed: {exc}")

        log.info(
            f"AUTH:LDAP: Login successful for '{username}' "
            f"(dn={user_dn}, groups={len(groups)})."
        )
        return AuthResult(
            username=username,
            full_name=full_name,
            email=email,
            roles=roles,
            provider=self.provider_id,
            provider_user_id=user_dn,
        )

    async def test_connection(self) -> tuple[bool, str]:
        """Verify service-account bind. Returns (ok, message) for the settings UI."""
        try:
            import ldap3
        except ImportError:
            return False, "ldap3 not installed"

        if not self.is_configured():
            return False, "Provider not fully configured"

        try:
            use_ssl = self.url.lower().startswith("ldaps://")
            tls = ldap3.Tls(validate=2 if self.tls_verify else 0)
            server = ldap3.Server(self.url, get_info=ldap3.ALL, use_ssl=use_ssl, tls=tls)
            conn = ldap3.Connection(
                server, user=self.bind_dn, password=self.bind_password, auto_bind=True
            )
            conn.unbind()
            return True, f"Connected to {self.url}"
        except Exception as e:
            return False, str(e)
