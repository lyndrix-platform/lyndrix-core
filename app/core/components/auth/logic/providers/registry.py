from typing import Optional, List

from core.bus import bus
from core.logger import get_logger
from .base import AuthProvider, AuthResult

log = get_logger("Core:AuthRegistry")


class AuthProviderRegistry:
    """
    Central registry for all authentication providers.

    Providers are tried in registration order.  Plugins can add custom
    providers at runtime by emitting the 'auth:register_provider' bus event
    with a payload of {'provider': <AuthProvider instance>}.
    """

    def __init__(self):
        # Ordered dict preserves insertion order (Python 3.7+)
        self._providers: dict[str, AuthProvider] = {}
        bus.subscribe("auth:register_provider")(self._on_bus_register)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, provider: AuthProvider) -> None:
        if not isinstance(provider, AuthProvider):
            log.warning("AUTH: register() called with non-AuthProvider object — ignored.")
            return
        self._providers[provider.provider_id] = provider
        log.info(f"AUTH: Provider registered: '{provider.provider_id}' ({provider.display_name})")

    def unregister(self, provider_id: str) -> None:
        if provider_id in self._providers:
            del self._providers[provider_id]
            log.info(f"AUTH: Provider unregistered: '{provider_id}'")

    def clear(self) -> None:
        """Remove all registered providers (used before re-initialization)."""
        ids = list(self._providers.keys())
        self._providers.clear()
        if ids:
            log.info(f"AUTH: Registry cleared (removed: {ids}).")

    async def _on_bus_register(self, payload: dict) -> None:
        provider = (payload or {}).get("provider")
        if isinstance(provider, AuthProvider):
            self.register(provider)
        else:
            log.warning("AUTH: 'auth:register_provider' event received with invalid payload.")

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def get(self, provider_id: str) -> Optional[AuthProvider]:
        return self._providers.get(provider_id)

    def get_all(self) -> List[AuthProvider]:
        return list(self._providers.values())

    def get_form_providers(self) -> List[AuthProvider]:
        """Configured providers that accept username/password."""
        return [p for p in self._providers.values() if not p.is_sso() and p.is_configured()]

    def get_sso_providers(self) -> List[AuthProvider]:
        """Configured providers that use browser-redirect flow."""
        return [p for p in self._providers.values() if p.is_sso() and p.is_configured()]

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, username: str, password: str) -> Optional[AuthResult]:
        """
        Try each configured form-based provider in registration order.
        Returns the first successful AuthResult or None if all fail.

        Safety net: if no provider succeeds and the local provider is not already
        in the chain, attempt local authentication as a last-resort fallback so
        the built-in admin account remains accessible even when external providers
        (LDAP, OIDC) are misconfigured or unreachable.
        """
        for provider in self.get_form_providers():
            try:
                result = await provider.authenticate(username, password)
                if result:
                    return self._enrich_with_local_data(result)
            except Exception as e:
                log.error(
                    f"AUTH: Provider '{provider.provider_id}' raised an exception: {e}",
                    exc_info=True,
                )

        # Fallback: try local provider if it wasn't part of the active chain.
        if "local" not in self._providers:
            try:
                from .local import LocalProvider
                fallback = LocalProvider()
                result = await fallback.authenticate(username, password)
                if result:
                    log.warning(
                        f"AUTH: All configured providers failed; local fallback succeeded "
                        f"for '{username}'. Consider adding 'local' to LYNDRIX_AUTH_PROVIDERS."
                    )
                    return self._enrich_with_local_data(result)
            except Exception as e:
                log.error(f"AUTH: Local fallback raised an exception: {e}", exc_info=True)

        return None

    def _enrich_with_local_data(self, result: AuthResult) -> AuthResult:
        """
        After successful authentication (any provider), merge in the user's
        explicit local group memberships and extra_permissions from the DB.
        This ensures that roles assigned via the Groups UI take effect for
        LDAP and OIDC users too.
        """
        try:
            from core.components.database.logic.db_service import db_instance
            from core.components.auth.logic.models import User
            if not db_instance.SessionLocal:
                return result
            with db_instance.SessionLocal() as s:
                user = s.query(User).filter(User.username == result.username).first()
                if user:
                    extra_groups = list(user.groups or [])
                    extra_perms  = list(user.extra_permissions or [])
                    merged_roles = sorted(set(result.roles) | set(extra_groups))
                    return AuthResult(
                        username=result.username,
                        full_name=result.full_name,
                        email=result.email,
                        roles=merged_roles,
                        provider=result.provider,
                        provider_user_id=result.provider_user_id,
                        extra_permissions=extra_perms,
                    )
        except Exception as e:
            log.warning(f"AUTH: Could not enrich result with local data: {e}")
        return result


# Singleton — imported by auth_service and login_ui
provider_registry = AuthProviderRegistry()
