"""Socket Manager - extensible socket management with security guards."""

from .logic.base_socket_provider import BaseSocketProvider, ProviderCapabilities
from .logic.mount_guardian import MountGuardian
from .providers.docker_provider import DockerProvider
from .registry import get_registry, SocketProviderRegistry
from .models.socket_models import (
    SpawnResult,
    MountInfo,
    EnvironmentVar,
    MountStatus,
    HealthCheckResult,
)

__all__ = [
    "BaseSocketProvider",
    "ProviderCapabilities",
    "MountGuardian",
    "DockerProvider",
    "SocketProviderRegistry",
    "get_registry",
    "SpawnResult",
    "MountInfo",
    "EnvironmentVar",
    "MountStatus",
    "HealthCheckResult",
]
