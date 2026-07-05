import asyncio
from typing import Optional, List, Dict

from core.logger import get_logger
from .base import AuthProvider, AuthResult

log = get_logger("Auth:LDAPProvider")

# Client-side timeout (seconds) for LDAP connect/bind/search. Without this a
# slow or unreachable directory would hang the worker thread indefinitely.
_LDAP_TIMEOUT = 10


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
        group_mapping: Optional[Dict[str, List[str]]] = None,
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
        self.group_mapping = group_mapping or {}
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

    def _map_groups_to_local_groups(self, groups: List[str]) -> List[str]:
        """Resolve directory group DNs to local Lyndrix group names via the
        admin-configured ``group_mapping`` (LYNDRIX_LDAP_GROUP_MAPPING)."""
        local: set = set()
        for group in groups:
            local.update(self.group_mapping.get(group, []))
        return sorted(local)

    def _authenticate_blocking(self, username: str, password: str) -> Optional[dict]:
        """Run the synchronous ldap3 bind/search and return resolved attributes.

        Executed in a worker thread by ``authenticate`` so the blocking network
        I/O never runs on the event loop. Returns a dict of directory attributes
        on success, or ``None`` on any auth/connection failure.
        """
        import ldap3
        import ldap3.utils.conv

        # Prevent LDAP injection in the search filter
        safe_username = ldap3.utils.conv.escape_filter_chars(username)
        user_filter = self.user_filter.replace("{username}", safe_username)

        use_ssl = self.url.lower().startswith("ldaps://")
        tls = ldap3.Tls(validate=2 if self.tls_verify else 0)
        server = ldap3.Server(
            self.url,
            get_info=ldap3.ALL,
            use_ssl=use_ssl,
            tls=tls,
            connect_timeout=_LDAP_TIMEOUT,
        )

        try:
            # Step 1: service-account bind to locate user DN
            svc_conn = ldap3.Connection(
                server,
                user=self.bind_dn,
                password=self.bind_password,
                auto_bind=True,
                receive_timeout=_LDAP_TIMEOUT,
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
                    # Stable AD account id — preferred provider_user_id for
                    # identity linking (survives renames/DN moves). ldap3's AD
                    # formatter yields the canonical S-1-5-… string.
                    "objectSid",
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
            user_conn = ldap3.Connection(
                server, user=user_dn, password=password,
                receive_timeout=_LDAP_TIMEOUT,
            )
            if not user_conn.bind():
                log.warning(f"AUTH:LDAP: Incorrect password for '{username}'.")
                return None
            user_conn.unbind()

        except ldap3.core.exceptions.LDAPException as e:
            log.error(f"AUTH:LDAP: Connection/search error: {e}", exc_info=True)
            return None

        # Step 3: extract directory attributes (still inside the worker thread).
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
        sid_val = entry["objectSid"].value if "objectSid" in entry.entry_attributes else None
        return {
            "user_dn": user_dn,
            "full_name": full_name,
            "email": email,
            # True only when the directory actually carried a mail attribute —
            # the synthetic "<user>@ldap.local" fallback must never count as a
            # verified email for identity auto-linking.
            "email_is_real": bool(email_val),
            "groups": [str(g) for g in raw_groups],
            "object_sid": str(sid_val) if sid_val else None,
        }

    async def authenticate(self, username: str, password: str) -> Optional[AuthResult]:
        try:
            import ldap3  # noqa: F401 — verify dependency before threading
        except ImportError:
            log.error("AUTH:LDAP: ldap3 package not available.")
            return None

        if not self.is_configured():
            log.warning("AUTH:LDAP: Provider not fully configured.")
            return None

        # ldap3 is fully synchronous; offload the bind/search to a worker thread so
        # a slow/unreachable directory cannot stall the whole event loop.
        resolved = await asyncio.to_thread(
            self._authenticate_blocking, username, password
        )
        if resolved is None:
            return None

        user_dn = resolved["user_dn"]
        full_name = resolved["full_name"]
        email = resolved["email"]
        groups = resolved["groups"]
        roles = self._map_groups_to_roles(groups)

        # Resolve directory groups to local Lyndrix GROUP names — groups are the
        # permission carriers, so this is what actually grants access on login.
        # Two sources, unioned: (1) the admin's LYNDRIX_LDAP_GROUP_MAPPING config,
        # (2) each local group's own ldap_mappings (edited in the Groups UI).
        local_groups: set = set(self._map_groups_to_local_groups(groups))
        try:
            from core.components.auth.logic.group_service import group_service
            local_groups |= set(group_service.resolve_ldap_groups(groups))
        except Exception as exc:
            log.warning(f"AUTH:LDAP: local group resolution failed: {exc}")

        log.info(
            f"AUTH:LDAP: Login successful for '{username}' "
            f"(dn={user_dn}, groups={len(groups)}, local_groups={sorted(local_groups)})."
        )
        return AuthResult(
            username=username,
            full_name=full_name,
            email=email,
            roles=roles,
            groups=sorted(local_groups),
            provider=self.provider_id,
            # objectSid preferred (rename/move-proof); DN fallback for non-AD.
            provider_user_id=resolved.get("object_sid") or user_dn,
            # Directory attributes are admin-controlled — trust the email for
            # identity auto-linking, but only when it really came from the
            # directory (not the synthetic @ldap.local fallback).
            email_verified=bool(resolved.get("email_is_real")),
        )

    def _test_connection_blocking(self) -> tuple[bool, str]:
        import ldap3

        try:
            use_ssl = self.url.lower().startswith("ldaps://")
            tls = ldap3.Tls(validate=2 if self.tls_verify else 0)
            server = ldap3.Server(
                self.url,
                get_info=ldap3.ALL,
                use_ssl=use_ssl,
                tls=tls,
                connect_timeout=_LDAP_TIMEOUT,
            )
            conn = ldap3.Connection(
                server, user=self.bind_dn, password=self.bind_password,
                auto_bind=True, receive_timeout=_LDAP_TIMEOUT,
            )
            conn.unbind()
            return True, f"Connected to {self.url}"
        except Exception as e:
            return False, str(e)

    async def test_connection(self) -> tuple[bool, str]:
        """Verify service-account bind. Returns (ok, message) for the settings UI."""
        try:
            import ldap3  # noqa: F401 — verify dependency before threading
        except ImportError:
            return False, "ldap3 not installed"

        if not self.is_configured():
            return False, "Provider not fully configured"

        # Offload the blocking ldap3 connect/bind off the event loop.
        return await asyncio.to_thread(self._test_connection_blocking)
