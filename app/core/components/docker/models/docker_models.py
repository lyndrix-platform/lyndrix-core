"""Data models for Docker socket management."""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class MountInfo:
    """Information about a mounted volume."""
    target: str
    source: str
    mode: str = "rw"

    def to_dict(self) -> Dict:
        return {
            "target": self.target,
            "source": self.source,
            "mode": self.mode,
        }


@dataclass
class EnvironmentVar:
    """Environment variable for container."""
    key: str
    value: str

    def to_dict(self) -> Dict:
        return {"key": self.key, "value": self.value}


@dataclass
class SpawnResult:
    """Result of spawning a container."""
    container_id: str
    name: str
    status: str
    mounts: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "container_id": self.container_id,
            "name": self.name,
            "status": self.status,
            "mounts": self.mounts,
            "error": self.error,
        }


@dataclass
class MountStatus:
    """Status of a mount point."""
    path: str
    mounted: bool
    writable: bool
    owner: Optional[str] = None
    permissions: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "mounted": self.mounted,
            "writable": self.writable,
            "owner": self.owner,
            "permissions": self.permissions,
            "error": self.error,
        }


@dataclass
class HealthCheckResult:
    """Overall health status of Docker socket management."""
    healthy: bool
    mounts: Dict[str, MountStatus] = field(default_factory=dict)
    cleanup_errors: Optional[str] = None
    repairs_made: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "healthy": self.healthy,
            "mounts": {k: v.to_dict() for k, v in self.mounts.items()},
            "cleanup_errors": self.cleanup_errors,
            "repairs_made": self.repairs_made,
        }
