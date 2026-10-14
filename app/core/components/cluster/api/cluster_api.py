"""Liveness/readiness probes and the cluster inspection API (docs/ha/operations/cluster-status.md).

* ``GET /api/live``  — process is up. Liveness probe; never depends on DB/Vault.
* ``GET /api/ready`` — 200 only when this instance may receive traffic (boot
  finished, DB + Vault connected, not draining, coordinator up in cluster mode,
  no hard preflight failure); otherwise 503 with the reasons. **The** health
  check for load balancers and Kubernetes readiness probes. Never use
  ``/api/health`` for that — it calls every plugin's ``health()``.
* ``GET /api/cluster`` — instances, leader, leases, placements, preflight.
* ``GET /api/cluster/preflight`` — re-run the preflight now.
* ``POST /api/cluster/leases/{name}/release`` — hand a lease over (admin).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.api.security import ApiIdentity, require_permission
from core.components.cluster.logic.cluster_service import cluster_service

cluster_router = APIRouter(tags=["Cluster"])

_NO_STORE = {"Cache-Control": "no-store, max-age=0"}


@cluster_router.get("/api/live", summary="Liveness probe")
async def live():
    return JSONResponse({"status": "alive", "instance_id": cluster_service.instance_id}, headers=_NO_STORE)


@cluster_router.get("/api/ready", summary="Readiness probe (load balancer health check)")
async def ready():
    ok, reasons = cluster_service.readiness()
    body = {
        "ready": ok,
        "reasons": reasons,
        "instance_id": cluster_service.instance_id,
        "mode": cluster_service.mode,
        "leader": cluster_service.is_leader,
        "draining": cluster_service.draining,
    }
    return JSONResponse(body, status_code=200 if ok else 503, headers=_NO_STORE)


@cluster_router.get("/api/cluster", summary="Cluster status: instances, leader, leases, placements")
async def cluster_status(identity: ApiIdentity = Depends(require_permission("api:read"))):
    return await cluster_service.describe()


@cluster_router.get("/api/cluster/preflight", summary="Re-run the HA preflight checks")
async def cluster_preflight(identity: ApiIdentity = Depends(require_permission("api:read"))):
    checks = await cluster_service.run_preflight()
    return {"checks": checks, **cluster_service.preflight_summary(), "instance_id": cluster_service.instance_id}


@cluster_router.post("/api/cluster/leases/{name:path}/release", summary="Force-release a lease")
async def release_lease(name: str, identity: ApiIdentity = Depends(require_permission("admin:write"))):
    """Give ``name`` up so another instance can take it on its next heartbeat.

    Works for leases held here (``lost`` callbacks fire) and for stale rows of a
    dead instance. The leader lease moves within one heartbeat interval.
    """
    if not name or len(name) > 150:
        raise HTTPException(status_code=400, detail="invalid lease name")
    released = await cluster_service.force_release(name)
    return {"status": "released" if released else "not_held", "lease": name, "by": identity.username}
