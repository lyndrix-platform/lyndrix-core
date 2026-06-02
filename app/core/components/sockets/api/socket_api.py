"""Socket management API endpoints with permission guards."""

from fastapi import APIRouter, Query, HTTPException, Depends
from pydantic import BaseModel
from functools import lru_cache
import logging
from typing import Optional

from core.api import optional_api_auth, require_permission
from ..logic.mount_guardian import MountGuardian
from ..providers.docker_provider import DockerProvider
from ..registry import get_registry

logger = logging.getLogger(__name__)

socket_router = APIRouter(prefix="/api/socket", tags=["socket-management"])


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
    """Request body for repair endpoint."""
    target_dir: str
    user: str = "root"
    mode: int = 755


@socket_router.get("/health")
async def socket_health(
    required_dirs: Optional[str] = Query(None),
    auth=Depends(optional_api_auth),
):
    """
    Check socket health and mount points.
    
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
    auth=Depends(optional_api_auth),
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
async def docker_health(auth=Depends(optional_api_auth)):
    """Check Docker daemon health."""
    try:
        provider = get_docker_provider()
        health = await provider.health_check()
        return health
    except Exception as e:
        logger.error(f"Docker health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@socket_router.get("/docker/mounts")
async def docker_mounts(auth=Depends(optional_api_auth)):
    """Get Docker container mount mappings."""
    try:
        provider = get_docker_provider()
        mounts = await provider.get_mounts()
        return {"mounts": mounts}
    except Exception as e:
        logger.error(f"Failed to get Docker mounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@socket_router.get("/docker/storage-root")
async def docker_storage_root(auth=Depends(optional_api_auth)):
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
    """
    try:
        guardian = get_mount_guardian()
        error = guardian.repair_permissions(
            request.target_dir,
            user=request.user,
            mode=request.mode,
        )
        if error:
            raise HTTPException(status_code=500, detail=error)
        return {"status": "repaired"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Repair failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
