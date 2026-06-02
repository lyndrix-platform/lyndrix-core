"""Docker socket manager - handles container lifecycle and mount queries."""

import docker
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from ..models.docker_models import SpawnResult, MountInfo, EnvironmentVar


class DockerSocketManager:
    """Manages Docker socket interactions for spawning and querying containers."""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self._client = None
        self._storage_root_cache = None

    @property
    def client(self) -> docker.DockerClient:
        """Lazy-load Docker client."""
        if self._client is None:
            try:
                self._client = docker.from_env()
            except Exception as e:
                self.logger.error(f"Failed to connect to Docker socket: {e}")
                raise
        return self._client

    def spawn_runner(
        self,
        image: str,
        name: str,
        env_vars: Optional[List[EnvironmentVar]] = None,
        mounts: Optional[List[MountInfo]] = None,
        command: Optional[List[str]] = None,
        remove: bool = True,
        networks: Optional[List[str]] = None,
    ) -> SpawnResult:
        """
        Spawn a Docker runner container.

        Args:
            image: Docker image to run
            name: Container name
            env_vars: List of EnvironmentVar objects
            mounts: List of MountInfo (volume mounts)
            command: Command to run
            remove: Auto-remove container on exit
            networks: Networks to connect to

        Returns:
            SpawnResult with container_id, status, mounts
        """
        try:
            env_dict = {ev.key: ev.value for ev in (env_vars or [])}
            volumes_dict = {m.source: {"bind": m.target, "mode": m.mode} for m in (mounts or [])}

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

            # Extract mount info from container
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

    def cleanup_stale_runners(
        self, prefix: str = "aac-runner-", max_age_minutes: int = 60
    ) -> Tuple[int, Optional[str]]:
        """
        Clean up stale runner containers.

        Args:
            prefix: Container name prefix to match
            max_age_minutes: Remove containers older than this (minutes)

        Returns:
            Tuple of (count_removed, error_message)
        """
        try:
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
            if removed_count > 0:
                self.logger.info(f"Cleaned up {removed_count} stale runners")

            return removed_count, error_msg

        except Exception as e:
            self.logger.error(f"Failed to cleanup stale runners: {e}")
            return 0, str(e)

    def get_container_mounts(self) -> Dict[str, Dict[str, str]]:
        """
        Get mount mappings for all running containers.

        Returns:
            Dict {container_id: {"/data": "/host/path", ...}}
        """
        try:
            result = {}
            for container in self.client.containers.list():
                mounts = self._extract_container_mounts(container)
                if mounts:
                    result[container.id[:12]] = mounts
            return result
        except Exception as e:
            self.logger.error(f"Failed to get container mounts: {e}")
            return {}

    def get_storage_root_from_mount(self) -> Optional[str]:
        """
        Detect where storage is actually mounted inside a running container.
        Queries Docker to find a mount pattern like /data/storage.

        Returns:
            The host path where /data is mounted, or None if not found
        """
        if self._storage_root_cache:
            return self._storage_root_cache

        try:
            for container in self.client.containers.list():
                mounts = self._extract_container_mounts(container)
                if "/data/storage" in mounts or "/data" in mounts:
                    target = "/data/storage" if "/data/storage" in mounts else "/data"
                    self._storage_root_cache = mounts.get(target)
                    self.logger.info(f"Detected storage root: {self._storage_root_cache}")
                    return self._storage_root_cache
        except Exception as e:
            self.logger.error(f"Failed to detect storage root: {e}")

        return None

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
