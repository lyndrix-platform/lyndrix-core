"""Socket provider registry - allows plugins to register custom socket providers."""

import logging
from typing import Dict, Type, Optional
from .logic.base_socket_provider import BaseSocketProvider

log = logging.getLogger("Core:SocketRegistry")


class SocketProviderRegistry:
    """Registry for socket providers (core + plugin-provided)."""

    def __init__(self):
        self._providers: Dict[str, Type[BaseSocketProvider]] = {}
        self._instances: Dict[str, BaseSocketProvider] = {}

    def register(
        self,
        provider_class: Type[BaseSocketProvider],
        plugin_name: Optional[str] = None,
    ) -> bool:
        """
        Register a socket provider.

        Args:
            provider_class: Subclass of BaseSocketProvider
            plugin_name: Name of registering plugin (for logging)

        Returns:
            True if registered, False if already exists or invalid
        """
        # Validate it's a proper provider
        if not issubclass(provider_class, BaseSocketProvider):
            log.error(
                f"Invalid provider from {plugin_name or 'core'}: "
                f"must inherit from BaseSocketProvider"
            )
            return False

        # Instantiate to get the name
        try:
            instance = provider_class(logger=log)
            provider_name = instance.name
        except Exception as e:
            log.error(
                f"Failed to instantiate provider from {plugin_name or 'core'}: {e}"
            )
            return False

        # Check for duplicates
        if provider_name in self._providers:
            log.warning(
                f"Provider '{provider_name}' already registered "
                f"(from {plugin_name or 'core'}); skipping duplicate"
            )
            return False

        self._providers[provider_name] = provider_class
        self._instances[provider_name] = instance
        log.info(
            f"Registered socket provider '{provider_name}' from {plugin_name or 'core'}"
        )
        return True

    def get_provider(self, name: str) -> Optional[BaseSocketProvider]:
        """Get a provider instance by name."""
        return self._instances.get(name)

    def list_available(self) -> Dict[str, dict]:
        """List all available providers (only those with socket available)."""
        available = {}
        for name, instance in self._instances.items():
            if instance.is_available:
                caps = instance.capabilities
                available[name] = {
                    "socket_path": instance.socket_path,
                    "capabilities": {
                        "can_spawn": caps.can_spawn,
                        "can_cleanup": caps.can_cleanup,
                        "can_query_mounts": caps.can_query_mounts,
                        "can_verify_mounts": caps.can_verify_mounts,
                        "can_repair_permissions": caps.can_repair_permissions,
                    },
                }
        return available

    def list_all(self) -> Dict[str, dict]:
        """List all registered providers (whether available or not)."""
        all_providers = {}
        for name, instance in self._instances.items():
            caps = instance.capabilities
            all_providers[name] = {
                "socket_path": instance.socket_path,
                "available": instance.is_available,
                "capabilities": {
                    "can_spawn": caps.can_spawn,
                    "can_cleanup": caps.can_cleanup,
                    "can_query_mounts": caps.can_query_mounts,
                    "can_verify_mounts": caps.can_verify_mounts,
                    "can_repair_permissions": caps.can_repair_permissions,
                },
            }
        return all_providers


# Global registry instance
_socket_registry = SocketProviderRegistry()


def get_registry() -> SocketProviderRegistry:
    """Get the global socket provider registry."""
    return _socket_registry
