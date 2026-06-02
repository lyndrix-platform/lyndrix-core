"""Docker socket provider implementation."""

import docker
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..logic.base_socket_provider import BaseSocketProvider, ProviderCapabilities
from ..models.socket_models import SpawnResult, MountInfo, EnvironmentVar


class DockerProvider(BaseSocketProvider):
    """Docker socket provider for container management."""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self._client = None

    @property
    def name(self) -> str:
        return "docker"

    @property
    def socket_path(self) -> str:
        return "/var/run/docker.sock"

    @property
    def is_available(self) -> bool:
        return Path(self.socket_path).exists() and self.client is not None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_spawn=True,
            can_cleanup=True,
            can_query_mounts=True,
            can_verify_mounts=True,
            can_repair_permissions=False,  # Handled by mount_guardian
        )

    @property
    def client(self) -> docker.DockerClient:
        """Lazy-load Docker client."""
        if self._client is None:
            try:
                self._client = docker.from_env()
            except Exception as e:
                self.logger.error(f"Failed to connect to Docker socket: {e}")
                return None
        return self._client

    async def health_check(self) -> Dict:
        """Check Docker daemon health."""
        try:
            if not self.client:
                return {"healthy": False, "error": "Docker client unavailable"}
            info = self.client.info()
            return {
                "healthy": True,
                "containers_running": info.get("ContainersRunning", 0),
                "containers_total": info.get("Containers", 0),
                "images": info.get("Images", 0),
            }
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    async def cleanup_stale(
        self, prefix: str = "aac-runner-", max_age_minutes: int = 60
    ) -> Tuple[int, Optional[str]]:
        """Clean up stale Docker containers."""
        try:
            if not self.client:
                return 0, "Docker client unavailable"

            cutoff_time = datetime.now() - timedelta(minutes=max_age_minutes)
            removed_count = 0
            errors = []

            for container in self.client.containers.list(all=True):
                if not container.name.startswith(prefix):
                    continue

                created = datetime.fromisoformat(
                    container.attrs["Created"].replace("Z", "+00:00")
                )
                if created < cutoff_time:
                    try:
                        if container.status == "running":
                            container.stop(timeout=5)
                        container.remove(force=True)
                        self.logger.info(f"Removed stale container: {container.name}")
                        removed_count += 1
                    except Exception as e:
                        errors.append(f"{container.name}: {str(e)}")

            error_msg = "; ".join(errors) if errors else None
            return removed_count, error_msg

        except Exception as e:
            return 0, str(e)

    async def get_mounts(self) -> Dict[str, Dict[str, str]]:
        """Get mount mappings for all running containers."""
        try:
            if not self.client:
                return {}

            result = {}
            for container in self.client.containers.list():
                mounts = self._extract_container_mounts(container)
                if mounts:
                    result[container.id[:12]] = mounts
            return result
        except Exception as e:
            self.logger.error(f"Failed to get container mounts: {e}")
            return {}

    async def detect_storage_root(self) -> Optional[str]:
        """Detect where storage is mounted inside containers."""
        try:
            if not self.client:
                return None

            for container in self.client.containers.list():
                mounts = self._extract_container_mounts(container)
                if "/data/storage" in mounts:
                    return mounts["/data/storage"]
                if "/data" in mounts:
                    return mounts["/data"]
        except Exception as e:
            self.logger.error(f"Failed to detect storage root: {e}")

        return None

    async def spawn_runner(
        self,
        image: str,
        name: str,
        env_vars: Optional[List[EnvironmentVar]] = None,
        mounts: Optional[List[MountInfo]] = None,
        command: Optional[List[str]] = None,
        remove: bool = True,
        networks: Optional[List[str]] = None,
    ) -> SpawnResult:
        """Spawn a Docker container."""
        try:
            if not self.client:
                return SpawnResult(
                    container_id="",
                    name=name,
                    status="failed",
                    error="Docker client unavailable",
                )

            env_dict = {ev.key: ev.value for ev in (env_vars or [])}
            volumes_dict = {
                m.source: {"bind": m.target, "mode": m.mode} for m in (mounts or [])
            }

            container = self.client.containers.run(
                image,
                command=command,
                name=name,
                environment=env_dict,
                volumes=volumes_dict,
                detach=True,
                remove=remove,
                network=networks[0] if networks else None,
            )

            self.logger.info(f"Spawned container {name} (ID: {container.id[:12]})")
            container_mounts = self._extract_container_mounts(container)

            return SpawnResult(
                container_id=container.id,
                name=name,
                status="running",
                mounts=container_mounts,
            )

        except Exception as e:
            self.logger.error(f"Failed to spawn container {name}: {e}")
            return SpawnResult(
                container_id="",
                name=name,
                status="failed",
                error=str(e),
            )

    @staticmethod
    def _extract_container_mounts(container) -> Dict[str, str]:
        """Extract mount mappings from a container."""
        mounts = {}
        try:
            for mount in container.attrs.get("Mounts", []):
                target = mount.get("Destination")
                source = mount.get("Source")
                if target and source:
                    mounts[target] = source
        except Exception:
            pass
        return mounts
