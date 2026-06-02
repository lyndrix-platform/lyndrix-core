"""Mount guardian - verifies and repairs mount points and permissions."""

import logging
import os
import subprocess
import stat
from typing import Dict, List, Optional

from ..models.socket_models import MountStatus, HealthCheckResult


class MountGuardian:
    """Verifies mount points and auto-repairs permissions/ownership issues."""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    def verify_mounts(self, required_dirs: List[str]) -> Dict[str, MountStatus]:
        """
        Verify that required directories are mounted and writable.

        Args:
            required_dirs: List of paths to check

        Returns:
            Dict {path: MountStatus}
        """
        results = {}
        for directory in required_dirs:
            results[directory] = self._check_mount(directory)
        return results

    def repair_permissions(
        self, target_dir: str, user: str = "root", mode: int = 0o755
    ) -> Optional[str]:
        """
        Attempt to repair permissions and ownership on a directory.

        Args:
            target_dir: Directory to repair
            user: Username/UID to own the directory
            mode: Permission mode (octal)

        Returns:
            Error message if repair failed, None if successful
        """
        if not os.path.exists(target_dir):
            self.logger.warning(f"Directory does not exist: {target_dir}")
            return f"Directory does not exist: {target_dir}"

        try:
            result = subprocess.run(
                ["sudo", "chown", "-R", user, target_dir],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                error_msg = f"chown failed: {result.stderr}"
                self.logger.error(error_msg)
                return error_msg

            result = subprocess.run(
                ["sudo", "chmod", "-R", oct(mode)[2:], target_dir],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                error_msg = f"chmod failed: {result.stderr}"
                self.logger.error(error_msg)
                return error_msg

            self.logger.info(f"Repaired permissions on {target_dir}")
            return None

        except subprocess.TimeoutExpired:
            return f"Permission repair timed out for {target_dir}"
        except Exception as e:
            return f"Failed to repair {target_dir}: {str(e)}"

    def get_health_status(
        self, required_dirs: List[str]
    ) -> HealthCheckResult:
        """
        Get overall health status and attempt repairs.

        Args:
            required_dirs: Directories to check

        Returns:
            HealthCheckResult with mount statuses and repairs made
        """
        mount_statuses = self.verify_mounts(required_dirs)
        repairs_made = {}

        for path, status in mount_statuses.items():
            if not status.healthy and not status.error:
                error = self.repair_permissions(path)
                if error is None:
                    repairs_made[path] = "permissions_repaired"
                    status.writable = True

        overall_healthy = all(
            s.mounted and s.writable for s in mount_statuses.values()
        )

        return HealthCheckResult(
            healthy=overall_healthy,
            mounts=mount_statuses,
            repairs_made=repairs_made,
        )

    def _check_mount(self, directory: str) -> MountStatus:
        """Check a single mount point."""
        try:
            if not os.path.exists(directory):
                return MountStatus(
                    path=directory,
                    mounted=False,
                    writable=False,
                    error="Directory does not exist",
                )

            try:
                dir_stat = os.stat(directory)
                parent_stat = os.stat(os.path.dirname(directory))
                is_mounted = dir_stat.st_dev != parent_stat.st_dev
            except Exception:
                is_mounted = True

            is_writable = os.access(directory, os.W_OK)

            try:
                dir_stat = os.stat(directory)
                owner_uid = dir_stat.st_uid
                owner = self._get_username(owner_uid)
                permissions = oct(stat.S_IMODE(dir_stat.st_mode))[2:]
            except Exception:
                owner = None
                permissions = None

            status = MountStatus(
                path=directory,
                mounted=is_mounted,
                writable=is_writable,
                owner=owner,
                permissions=permissions,
            )

            return status

        except Exception as e:
            return MountStatus(
                path=directory,
                mounted=False,
                writable=False,
                error=str(e),
            )

    @staticmethod
    def _get_username(uid: int) -> str:
        """Get username from UID, fallback to numeric UID."""
        try:
            import pwd

            return pwd.getpwuid(uid).pw_name
        except Exception:
            return str(uid)
