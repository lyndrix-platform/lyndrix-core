"""Docker socket management API endpoints (FastAPI)."""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from functools import lru_cache
import logging
from typing import Optional, Dict

from ..logic.docker_socket_manager import DockerSocketManager
from ..logic.mount_guardian import MountGuardian

logger = logging.getLogger(__name__)

docker_router = APIRouter(prefix="/api/docker", tags=["docker"])


@lru_cache(maxsize=1)
def get_socket_manager():
    """Cached socket manager instance."""
    return DockerSocketManager(logger=logger)


@lru_cache(maxsize=1)
def get_mount_guardian():
    """Cached mount guardian instance."""
    return MountGuardian(logger=logger)


class CleanupRequest(BaseModel):
    """Request body for cleanup endpoint."""
    prefix: str = "aac-runner-"
    max_age_minutes: int = 60


class RepairRequest(BaseModel):
    """Request body for repair endpoint."""
    target_dir: str
    user: str = "root"
    mode: int = 755


@docker_router.get("/health")
async def docker_health(required_dirs: Optional[str] = Query(None)):
    """
    Check Docker socket health and mount points.
    
    Query params:
    - required_dirs: Comma-separated list of paths to verify
    
    Example: /api/docker/health?required_dirs=/data/storage/git_repos,/data/security
    """
    try:
        dirs_list = []
        if required_dirs:
            dirs_list = [d.strip() for d in required_dirs.split(",") if d.strip()]

        guardian = get_mount_guardian()
        health = guardian.get_health_status(dirs_list)

        status_code = 200 if health.healthy else 503
        raise HTTPException(
            status_code=status_code,
            detail=health.to_dict(),
        ) if not health.healthy else None

        return health.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@docker_router.get("/mounts")
async def get_container_mounts():
    """Get mount mappings for all running containers."""
    try:
        manager = get_socket_manager()
        mounts = manager.get_container_mounts()
        return {"mounts": mounts}
    except Exception as e:
        logger.error(f"Failed to get container mounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@docker_router.get("/storage-root")
async def get_storage_root():
    """Detect where storage is mounted inside containers."""
    try:
        manager = get_socket_manager()
        storage_root = manager.get_storage_root_from_mount()
        if not storage_root:
            raise HTTPException(status_code=404, detail="Storage root not detected")
        return {"storage_root": storage_root}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to detect storage root: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@docker_router.post("/cleanup")
async def cleanup_stale_containers(request: CleanupRequest):
    """
    Clean up stale runner containers.
    
    Body:
    {
        "prefix": "aac-runner-",
        "max_age_minutes": 60
    }
    """
    try:
        manager = get_socket_manager()
        count, error = manager.cleanup_stale_runners(
            prefix=request.prefix, max_age_minutes=request.max_age_minutes
        )
        return {"removed": count, "error": error}
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@docker_router.post("/repair")
async def repair_mount_permissions(request: RepairRequest):
    """
    Repair permissions on a directory.
    
    Body:
    {
        "target_dir": "/data/storage/git_repos",
        "user": "root",
        "mode": 755
    }
    """
    try:
        guardian = get_mount_guardian()
        error = guardian.repair_permissions(
            request.target_dir, user=request.user, mode=request.mode
        )
        if error:
            raise HTTPException(status_code=500, detail=error)
        return {"status": "repaired"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Repair failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
