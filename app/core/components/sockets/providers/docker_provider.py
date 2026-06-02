"""Docker socket provider implementation."""

import asyncio
import json
import logging
import os
import shutil
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..logic.base_socket_provider import BaseSocketProvider, ProviderCapabilities
from ..models.socket_models import SpawnResult, MountInfo, EnvironmentVar


class DockerProvider(BaseSocketProvider):
    """Docker socket provider for container management."""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    @property
    def name(self) -> str:
        return "docker"

    @property
    def socket_path(self) -> str:
        return "/var/run/docker.sock"

    @property
    def is_available(self) -> bool:
        return Path(self.socket_path).exists() and shutil.which("docker") is not None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_spawn=True,
            can_cleanup=True,
            can_query_mounts=True,
            can_verify_mounts=True,
            can_repair_permissions=False,
        )

    async def health_check(self) -> Dict:
        """Check Docker daemon health."""
        if not self.is_available:
            return {"healthy": False, "error": "Docker socket or CLI unavailable"}
        code, stdout, stderr = await self._run_docker("info", "--format", "{{json .}}")
        if code != 0:
            return {"healthy": False, "error": stderr or stdout or "docker info failed"}
        try:
            info = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            info = {}
        return {
            "healthy": True,
            "containers_running": info.get("ContainersRunning", 0),
            "containers_total": info.get("Containers", 0),
            "images": info.get("Images", 0),
        }

    async def cleanup_stale(
        self, prefix: str = "aac-runner-", max_age_minutes: int = 60
    ) -> Tuple[int, Optional[str]]:
        """Clean up stale Docker containers."""
        if not self.is_available:
            return 0, "Docker socket or CLI unavailable"

        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        removed_count = 0
        errors = []

        code, stdout, stderr = await self._run_docker(
            "ps",
            "-a",
            "--filter",
            f"name=^{prefix}",
            "--format",
            "{{.ID}}|{{.Names}}|{{.Status}}|{{.Label \"iac_job_id\"}}|{{.Label \"iac_task_name\"}}",
        )
        if code != 0:
            return 0, stderr or stdout or "docker ps failed"

        for line in [l.strip() for l in stdout.splitlines() if l.strip()]:
            parts = line.split("|")
            if len(parts) < 1:
                continue
            container_id = parts[0]
            container_name = parts[1] if len(parts) > 1 else container_id
            try:
                created_at = await self._get_container_created_at(container_id)
                if created_at < cutoff_time:
                    await self._remove_container(container_id)
                    self.logger.info(f"Removed stale container: {container_name}")
                    removed_count += 1
            except Exception as e:
                errors.append(f"{container_name}: {str(e)}")

        error_msg = "; ".join(errors) if errors else None
        return removed_count, error_msg

    async def list_containers(self, prefix: str = "aac-runner-") -> list[dict]:
        """List containers that match a prefix, including runner labels."""
        if not self.is_available:
            return []

        code, stdout, stderr = await self._run_docker(
            "ps",
            "-a",
            "--filter",
            f"name=^{prefix}",
            "--format",
            "{{.ID}}|{{.Names}}|{{.Label \"iac_job_id\"}}|{{.Label \"iac_task_name\"}}|{{.Status}}",
        )
        if code != 0:
            self.logger.error(f"Failed to list containers: {stderr or stdout}")
            return []

        result = []
        for line in [l.strip() for l in stdout.splitlines() if l.strip()]:
            parts = line.split("|")
            if not parts:
                continue
            result.append(
                {
                    "id": parts[0],
                    "name": parts[1] if len(parts) > 1 else parts[0],
                    "job_id": parts[2] if len(parts) > 2 else "",
                    "task_name": parts[3] if len(parts) > 3 else "",
                    "status": parts[4] if len(parts) > 4 else "",
                    "labels": {
                        "iac_job_id": parts[2] if len(parts) > 2 else "",
                        "iac_task_name": parts[3] if len(parts) > 3 else "",
                    },
                }
            )
        return result

    async def get_mounts(self) -> Dict[str, Dict[str, str]]:
        """Get mount mappings for all running containers."""
        if not self.is_available:
            return {}

        result = {}
        code, stdout, stderr = await self._run_docker(
            "ps",
            "--format",
            "{{.ID}}|{{.Names}}",
        )
        if code != 0:
            self.logger.error(f"Failed to list running containers: {stderr or stdout}")
            return {}

        for line in [l.strip() for l in stdout.splitlines() if l.strip()]:
            cid = line.split("|", 1)[0]
            mounts = await self._inspect_mounts(cid)
            if mounts:
                result[cid[:12]] = mounts
        return result

    async def detect_storage_root(self) -> Optional[str]:
        """Detect where storage is mounted inside the current container."""
        mounts = await self.resolve_current_mounts(["/data/storage", "/data"])
        return mounts.get("/data/storage") or mounts.get("/data")

    async def resolve_current_mounts(self, required_targets: Optional[List[str]] = None) -> Dict[str, str]:
        """Resolve host sources for mount targets inside the current container."""
        mounts = await self._inspect_mounts(self._current_container_id())
        if not mounts:
            return {}

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
        import time
        start_time = time.monotonic()
        
        try:
            if not self.is_available:
                return SpawnResult(
                    container_id="",
                    name=name,
                    status="failed",
                    error="Docker socket or CLI unavailable",
                )

            await self._remove_container_by_name(name)

            cmd = ["run", "-d", "--name", name, "--pull", "always"]
            if remove:
                cmd.append("--rm")

            for ev in self._normalize_env_vars(env_vars or []):
                cmd.extend(["-e", f"{ev['key']}={ev['value']}"])

            for mount in self._normalize_mounts(mounts or []):
                cmd.extend(["-v", f"{mount['source']}:{mount['target']}:{mount['mode']}"])

            if networks:
                cmd.extend(["--network", networks[0]])

            cmd.extend(["--entrypoint", "", image])
            if command:
                cmd.extend(command)

            code, stdout, stderr = await self._run_docker(*cmd)
            if code != 0:
                elapsed = time.monotonic() - start_time
                return SpawnResult(
                    container_id="",
                    name=name,
                    status="failed",
                    error=stderr or stdout or "docker run failed",
                )

            container_id = stdout.strip()
            self.logger.info(f"Spawned container {name} (ID: {container_id[:12]})")
            container_mounts = await self._inspect_mounts(container_id)
            
            elapsed = time.monotonic() - start_time
            self.logger.debug(f"spawn_runner completed in {elapsed:.2f}s: {name}")

            return SpawnResult(
                container_id=container_id,
                name=name,
                status="running",
                mounts=container_mounts,
            )

        except Exception as e:
            elapsed = time.monotonic() - start_time
            self.logger.error(f"Failed to spawn container {name} after {elapsed:.2f}s: {e}")
            return SpawnResult(
                container_id="",
                name=name,
                status="failed",
                error=str(e),
            )

    @staticmethod
    def _normalize_env_vars(items: list) -> list[dict]:
        normalized = []
        for item in items:
            if isinstance(item, dict):
                key = item.get("key")
                value = item.get("value")
            else:
                key = getattr(item, "key", None)
                value = getattr(item, "value", None)
            if key is None or value is None:
                continue
            normalized.append({"key": str(key), "value": str(value)})
        return normalized

    @staticmethod
    def _normalize_mounts(items: list) -> list[dict]:
        normalized = []
        for item in items:
            if isinstance(item, dict):
                source = item.get("source")
                target = item.get("target")
                mode = item.get("mode", "rw")
            else:
                source = getattr(item, "source", None)
                target = getattr(item, "target", None)
                mode = getattr(item, "mode", "rw")
            if not source or not target:
                continue
            normalized.append({"source": str(source), "target": str(target), "mode": str(mode or "rw")})
        return normalized

    async def _run_docker(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    async def _remove_container(self, container_id_or_name: str) -> None:
        code, stdout, stderr = await self._run_docker("rm", "-f", container_id_or_name)
        if code != 0 and stderr:
            self.logger.debug(f"Container removal ignored for {container_id_or_name}: {stderr}")

    async def _remove_container_by_name(self, name: str) -> None:
        code, stdout, stderr = await self._run_docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}")
        if code != 0:
            return
        for container_id in [line.strip() for line in stdout.splitlines() if line.strip()]:
            await self._remove_container(container_id)

    async def _get_container_created_at(self, container_id: str) -> datetime:
        code, stdout, stderr = await self._run_docker(
            "inspect",
            "--format",
            "{{.Created}}",
            container_id,
        )
        if code != 0 or not stdout:
            raise RuntimeError(stderr or stdout or f"Unable to inspect container {container_id}")
        value = stdout.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(value)

    async def _inspect_mounts(self, container_id: Optional[str]) -> Dict[str, str]:
        if not container_id:
            return {}
        code, stdout, stderr = await self._run_docker(
            "inspect",
            "--format",
            "{{json .Mounts}}",
            container_id,
        )
        if code != 0 or not stdout:
            if stderr:
                self.logger.debug(f"Failed to inspect mounts for {container_id}: {stderr}")
            return {}
        try:
            mounts = json.loads(stdout)
        except json.JSONDecodeError:
            return {}
        result = {}
        for mount in mounts:
            target = mount.get("Destination")
            source = mount.get("Source")
            if target and source:
                result[target] = source
        return result

    def _current_container_id(self) -> Optional[str]:
        identifier = os.getenv("HOSTNAME") or socket.gethostname()
        if not identifier:
            return None
        return identifier

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
