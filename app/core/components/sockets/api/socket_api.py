"""Socket management API endpoints with permission guards."""

import asyncio
from fastapi import APIRouter, Query, HTTPException, Depends
from pydantic import BaseModel
from functools import lru_cache
import logging
from typing import Optional

from config import settings
from core.api import require_permission
from ..logic.mount_guardian import MountGuardian
from ..providers.docker_provider import DockerProvider
from ..registry import get_registry

logger = logging.getLogger(__name__)

socket_router = APIRouter(prefix="/api/socket", tags=["socket-management"])


def _repair_allowlist() -> list[str]:
    """Directories that may be targeted by the (privileged) repair endpoint.

    Restricting repair to the platform's own managed mounts prevents an
    authenticated caller from chown/chmod-ing arbitrary host paths.
    """
    return [
        settings.STORAGE_DIR,
        settings.SECURITY_DIR,
        settings.LOGS_DIR,
        settings.PLUGINS_DIR,
    ]


@lru_cache(maxsize=1)
def get_docker_provider():
    """Cached Docker provider instance."""
    return DockerProvider(logger=logger)


@lru_cache(maxsize=1)
def get_mount_guardian():
    """Cached mount guardian instance."""
    return MountGuardian(logger=logger)


class CleanupRequest(BaseModel):
    """Request body for cleanup endpoint."""
    prefix: str = "aac-runner-"
    max_age_minutes: int = 60


class RepairRequest(BaseModel):
    """Request body for repair endpoint.

    ``mode`` is interpreted as octal permission digits (e.g. 755 -> 0o755),
    matching the convention operators expect from chmod.
    """
    target_dir: str
    user: str = "root"
    mode: str = "755"


@socket_router.get("/health")
async def socket_health(
    required_dirs: Optional[str] = Query(None),
    auth=Depends(require_permission("admin:read")),
):
    """
    Check socket health and mount points (read-only inspection).

    This endpoint never mutates the filesystem — it only reports mount/permission
    status. Use POST /api/socket/repair (separate permission) to apply fixes.

    Query params:
    - required_dirs: Comma-separated list of paths to verify

    Example: /api/socket/health?required_dirs=/data/storage/git_repos,/data/security
    """
    try:
        dirs_list = []
        if required_dirs:
            dirs_list = [d.strip() for d in required_dirs.split(",") if d.strip()]

        guardian = get_mount_guardian()
        health = guardian.get_health_status(dirs_list)

        return health.to_dict()

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@socket_router.get("/providers")
async def list_providers(
    auth=Depends(require_permission("admin:read")),
):
    """List all registered socket providers (available and unavailable)."""
    registry = get_registry()
    all_providers = registry.list_all()
    available = registry.list_available()
    
    return {
        "providers": all_providers,
        "available": list(available.keys()),
        "total": len(all_providers),
    }


@socket_router.get("/docker/health")
async def docker_health(auth=Depends(require_permission("admin:read"))):
    """Check Docker daemon health."""
    try:
        provider = get_docker_provider()
        health = await provider.health_check()
        return health
    except Exception as e:
        logger.error(f"Docker health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@socket_router.get("/docker/mounts")
async def docker_mounts(auth=Depends(require_permission("admin:read"))):
    """Get Docker container mount mappings."""
    try:
        provider = get_docker_provider()
        mounts = await provider.get_mounts()
        return {"mounts": mounts}
    except Exception as e:
        logger.error(f"Failed to get Docker mounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@socket_router.get("/docker/storage-root")
async def docker_storage_root(auth=Depends(require_permission("admin:read"))):
    """Detect where storage is mounted inside Docker containers."""
    try:
        provider = get_docker_provider()
        storage_root = await provider.detect_storage_root()
        if not storage_root:
            raise HTTPException(status_code=404, detail="Storage root not detected")
        return {"storage_root": storage_root}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to detect storage root: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@socket_router.post("/docker/cleanup")
async def docker_cleanup(
    request: CleanupRequest,
    auth=Depends(require_permission("socket:container:cleanup")),
):
    """
    Clean up stale Docker runner containers (requires permission).
    """
    try:
        provider = get_docker_provider()
        count, error = await provider.cleanup_stale(
            prefix=request.prefix,
            max_age_minutes=request.max_age_minutes,
        )
        return {"removed": count, "error": error}
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@socket_router.post("/repair")
async def repair_mount_permissions(
    request: RepairRequest,
    auth=Depends(require_permission("socket:mounts:repair")),
):
    """
    Repair permissions on a directory (admin-only).

    The target is validated against an allowlist of platform-managed mounts so
    this cannot be used to chown/chmod arbitrary host paths. The subprocess runs
    off the event loop to avoid stalling all other clients during the repair.
    """
    # Validate the requested mode as octal digits (e.g. "755" -> 0o755). Passing
    # a bare decimal previously produced the wrong permissions (sticky bit etc.).
    try:
        mode_octal = int(str(request.mode), 8)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="mode must be octal permission digits, e.g. '755'",
        )

    # Validate the target against the managed-mount allowlist (resolve symlinks
    # and reject path traversal outside an allowed root).
    from pathlib import Path

    try:
        target = Path(request.target_dir).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid target_dir")

    allowed_roots = [Path(p).resolve() for p in _repair_allowlist()]
    if not any(target == root or target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(
            status_code=403,
            detail="target_dir is not within an allowed managed mount",
        )

    try:
        guardian = get_mount_guardian()
        # repair_permissions shells out to sudo chown/chmod (blocking); run it in
        # a worker thread so the event loop stays responsive.
        error = await asyncio.to_thread(
            guardian.repair_permissions,
            str(target),
            request.user,
            mode_octal,
        )
        if error:
            raise HTTPException(status_code=500, detail=error)
        return {"status": "repaired"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Repair failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
