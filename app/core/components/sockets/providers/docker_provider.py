"""Docker socket provider implementation."""

import docker
import logging
import os
import socket
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
                if not Path(self.socket_path).exists():
                    self.logger.error(f"Docker socket missing at {self.socket_path}")
                    return None
                self._client = docker.DockerClient(
                    base_url=f"unix://{self.socket_path}",
                    version="auto",
                )
                self._client.ping()
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

    async def list_containers(self, prefix: str = "aac-runner-") -> list[dict]:
        """List containers that match a prefix, including runner labels."""
        try:
            if not self.client:
                return []

            result = []
            for container in self.client.containers.list(all=True):
                name = getattr(container, "name", "")
                if not name.startswith(prefix):
                    continue
                labels = container.labels or {}
                result.append(
                    {
                        "name": name,
                        "id": container.id,
                        "status": container.status,
                        "labels": labels,
                    }
                )
            return result
        except Exception as e:
            self.logger.error(f"Failed to list containers: {e}")
            return []

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
            container = self._get_current_container()
            if not container:
                return None

            mounts = self._extract_container_mounts(container)
            if "/data/storage" in mounts:
                return mounts["/data/storage"]
            if "/data" in mounts:
                return mounts["/data"]
        except Exception as e:
            self.logger.error(f"Failed to detect storage root: {e}")

        return None

    async def resolve_current_mounts(self, required_targets: Optional[List[str]] = None) -> Dict[str, str]:
        """Resolve host sources for mount targets inside the current container."""
        container = self._get_current_container()
        if not container:
            return {}

        mounts = self._extract_container_mounts(container)
        targets = required_targets or [
            "/data/storage/git_repos",
            "/data/storage/services",
            "/data/storage/terraform-providers",
            "/data/security",
        ]
        resolved = {}
        for target in targets:
            source = self._resolve_mount_source(target, mounts)
            if source:
                resolved[target] = source
        return resolved

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

            existing = self._find_container_by_name(name)
            if existing is not None:
                try:
                    if existing.status == "running":
                        existing.stop(timeout=5)
                    existing.remove(force=True)
                except Exception as e:
                    self.logger.warning(f"Failed to remove existing container {name}: {e}")

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

    def _get_current_container(self):
        """Best-effort lookup for the running Lyndrix core container."""
        if not self.client:
            return None

        identifiers = [
            os.getenv("HOSTNAME"),
            socket.gethostname(),
        ]
        seen = set()
        for identifier in identifiers:
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            try:
                return self.client.containers.get(identifier)
            except Exception:
                pass

        current_id = socket.gethostname()
        for container in self.client.containers.list():
            cid = getattr(container, "id", "")
            if cid.startswith(current_id):
                return container
        return None

    def _find_container_by_name(self, name: str):
        if not self.client:
            return None
        try:
            return self.client.containers.get(name)
        except Exception:
            for container in self.client.containers.list(all=True):
                if getattr(container, "name", None) == name:
                    return container
        return None

    @staticmethod
    def _resolve_mount_source(target: str, mounts: Dict[str, str]) -> Optional[str]:
        """Resolve a target path to the best matching host source path."""
        target = target.rstrip("/")
        best_match = None
        best_len = -1
        for mount_target, mount_source in mounts.items():
            norm_target = mount_target.rstrip("/")
            if target == norm_target:
                return mount_source
            prefix = norm_target + "/"
            if target.startswith(prefix) and len(norm_target) > best_len:
                best_match = (norm_target, mount_source)
                best_len = len(norm_target)

        if not best_match:
            return None

        mount_target, mount_source = best_match
        suffix = target[len(mount_target):].lstrip("/")
        if not suffix:
            return mount_source
        return str(Path(mount_source) / suffix)
