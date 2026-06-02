"""Base class for socket providers (Docker, systemd, cgroup, etc.)."""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class ProviderCapabilities:
    """Declares what this provider can do."""
    can_spawn: bool = False
    can_cleanup: bool = False
    can_query_mounts: bool = False
    can_verify_mounts: bool = False
    can_repair_permissions: bool = False


class BaseSocketProvider(ABC):
    """Abstract base for socket providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'docker', 'systemd', 'cgroup')."""
        pass

    @property
    @abstractmethod
    def socket_path(self) -> str:
        """Path to the socket (e.g., '/var/run/docker.sock')."""
        pass

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """Check if socket is available on this system."""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Declare what this provider supports."""
        pass

    @abstractmethod
    async def health_check(self) -> Dict:
        """Return health status as dict."""
        pass

    @abstractmethod
    async def cleanup_stale(self, prefix: str, max_age_minutes: int) -> Tuple[int, Optional[str]]:
        """Clean up stale resources. Return (count_removed, error_msg)."""
        pass

    @abstractmethod
    async def get_mounts(self) -> Dict[str, Dict[str, str]]:
        """Get mount mappings. Return {resource_id: {target: source, ...}}."""
        pass

    @abstractmethod
    async def detect_storage_root(self) -> Optional[str]:
        """Auto-detect where storage is mounted. Return host path or None."""
        pass
