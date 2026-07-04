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
                    return await self.link_identity(result)
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
                    return await self.link_identity(result)
            except Exception as e:
                log.error(f"AUTH: Local fallback raised an exception: {e}", exc_info=True)

        return None

    async def link_identity(self, result: AuthResult) -> AuthResult:
        """Resolve the provider result to the linked LOCAL profile.

        Identity 2.0 replacement for the old ``_enrich_with_local_data``: every
        successful login (any provider, incl. SSO callbacks) flows through
        ``identity_link_service.resolve`` — creating/linking the local User +
        UserIdentity rows and rewriting the AuthResult onto the local username,
        system-flag roles and extra_permissions. Sync DB work → worker thread.
        """
        import asyncio

        try:
            from core.components.auth.logic import identity_link_service

            linked = await asyncio.to_thread(identity_link_service.resolve, result)
            return linked or result
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"AUTH: identity linking failed: {e}")
            return result


# Singleton — imported by auth_service and login_ui
provider_registry = AuthProviderRegistry()
