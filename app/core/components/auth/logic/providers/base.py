from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class AuthResult:
    """Unified result returned by every authentication provider."""
    username: str
    full_name: str
    email: str
    roles: List[str]
    provider: str
    provider_user_id: Optional[str] = None
    extra_permissions: List[str] = field(default_factory=list)


class AuthProvider(ABC):
    """
    Abstract base class for all authentication providers.

    Core providers (local, ldap, oidc) are shipped with Lyndrix.
    Plugins may register custom providers via the 'auth:register_provider' bus event:

        bus.emit("auth:register_provider", {"provider": MyProvider()})

    Or via ModuleContext (requires 'auth:register_provider' in permissions.emit):

        ctx.emit("auth:register_provider", {"provider": MyProvider()})
    """

    provider_id: str = ""
    display_name: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if all required configuration is present and the provider can be used."""
        ...

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> Optional[AuthResult]:
        """
        Verify credentials. Return an AuthResult on success, None on failure.
        SSO-only providers should return None here.
        """
        ...

    def is_sso(self) -> bool:
        """True if this provider uses browser-redirect flow (e.g. OIDC) instead of a form."""
        return False

    def get_login_url(self) -> Optional[str]:
        """
        For SSO providers: synchronously return the cached authorization redirect URL.
        Returns None until build_login_url() has been called at least once.
        """
        return None

    async def build_login_url(self) -> Optional[str]:
        """
        For SSO providers: fetch any remote metadata (e.g. OIDC discovery) as needed,
        generate a fresh state token, and return the full authorization redirect URL.
        """
        return None

    async def handle_callback(self, code: str, state: str) -> Optional[AuthResult]:
        """
        For SSO providers: exchange the authorization code for tokens and return an AuthResult.
        The result is stored internally; call consume_result(state) to retrieve it.
        """
        return None

    def consume_result(self, state: str) -> Optional[AuthResult]:
        """
        For SSO providers: pop and return the pending AuthResult for the given state token.
        Returns None if the state is unknown or already consumed.
        """
        return None
