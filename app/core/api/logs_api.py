"""Log tail API — serves the in-memory ring buffer to the React UI.

GET /api/logs?source=Plugin:Docker%20Manager&limit=200&level=WARNING
GET /api/logs/sources

``source`` is the exact logger name; the per-module convention is
``Plugin:{manifest.name}`` / ``Core:{manifest.name}`` (PluginOut carries the
precomputed ``log_source`` so clients never re-derive it). The buffer is
RAM-resident (LogRingBuffer) — reads are cheap copies, so a 1s polling client
costs nothing meaningful. Sync ``def`` routes (threadpool) by convention.
"""
from fastapi import APIRouter, Depends, Query

from core.api.security import ApiIdentity, require_permission
from core.logger import log_ring

logs_router = APIRouter(prefix="/api/logs", tags=["Logs"])


@logs_router.get("", summary="Tail in-memory logs (optionally per source)")
def tail_logs(
    source: str | None = Query(default=None, description="Exact logger name, e.g. 'Plugin:Docker Manager'"),
    limit: int = Query(default=200, ge=1, le=1000),
    level: str | None = Query(default=None, description="Minimum severity (DEBUG/INFO/WARNING/ERROR)"),
    identity: ApiIdentity = Depends(require_permission("api:read")),
):
    entries = log_ring.snapshot(source=source, limit=limit, min_level=level)
    return {
        "status": "ok",
        "source": source,
        "count": len(entries),
        "lines": [
            {"source": name, "level": lvl, "message": message}
            for name, lvl, message in entries
        ],
    }


@logs_router.get("/sources", summary="List log sources seen so far")
def list_log_sources(identity: ApiIdentity = Depends(require_permission("api:read"))):
    return {"status": "ok", "sources": log_ring.sources()}
