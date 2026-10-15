"""Log tail API — serves the in-memory ring buffer and the shared cluster log.

GET /api/logs?source=Plugin:Docker%20Manager&limit=200&level=WARNING
    → this instance's RAM ring buffer (fast path, exactly what reached the LB).
GET /api/logs?instance=<instance_id|all>&source=…&limit=…&level=…&since_id=…
    → the shared ``cluster_logs`` table: the log of ONE instance or of ALL of
      them merged, served by whichever instance answers. Lines carry ``instance``,
      ``ts`` and ``id`` (pass the last ``id`` as ``since_id`` to tail incrementally).
GET /api/logs/sources?instance=…      GET /api/logs/instances

``source`` is the exact logger name; the per-module convention is
``Plugin:{manifest.name}`` / ``Core:{manifest.name}`` (PluginOut carries the
precomputed ``log_source`` so clients never re-derive it). Sync ``def`` routes
(threadpool) by convention — the DB queries are small index range scans.
"""
from fastapi import APIRouter, Depends, Query

from core.api.security import ApiIdentity, require_permission
from core.logger import log_ring

logs_router = APIRouter(prefix="/api/logs", tags=["Logs"])


def _store():
    from core.components.cluster.logic.log_store import log_store

    return log_store


def _self_instance() -> str:
    try:
        from core.components.cluster.logic.cluster_service import cluster_service

        return cluster_service.instance_id
    except Exception:
        return "local"


@logs_router.get("", summary="Tail logs — this instance's ring buffer, or one/all instances from the shared store")
def tail_logs(
    source: str | None = Query(default=None, description="Exact logger name, e.g. 'Plugin:Docker Manager'"),
    limit: int = Query(default=200, ge=1, le=1000),
    level: str | None = Query(default=None, description="Minimum severity (DEBUG/INFO/WARNING/ERROR)"),
    instance: str | None = Query(default=None, description="Instance id or 'all' → shared cluster log; omit → local ring buffer"),
    since_id: int | None = Query(default=None, ge=0, description="Shared store only: return lines after this id"),
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    me = _self_instance()
    if instance:
        lines = _store().query(instance=instance, source=source, limit=limit, min_level=level, since_id=since_id)
        return {
            "status": "ok",
            "store": "db",
            "instance": instance,
            "answered_by": me,
            "source": source,
            "count": len(lines),
            "lines": lines,
        }
    entries = log_ring.snapshot(source=source, limit=limit, min_level=level)
    return {
        "status": "ok",
        "store": "memory",
        "instance": me,
        "answered_by": me,
        "source": source,
        "count": len(entries),
        "lines": [
            {"source": name, "level": lvl, "message": message, "instance": me}
            for name, lvl, message in entries
        ],
    }


@logs_router.get("/sources", summary="List log sources seen so far")
def list_log_sources(
    instance: str | None = Query(default=None, description="Instance id or 'all' → shared store; omit → local"),
    identity: ApiIdentity = Depends(require_permission("admin:read")),
):
    if instance:
        return {"status": "ok", "store": "db", "sources": _store().sources(instance)}
    return {"status": "ok", "store": "memory", "sources": log_ring.sources()}


@logs_router.get("/instances", summary="Instances that have shipped logs to the shared store")
def list_log_instances(identity: ApiIdentity = Depends(require_permission("admin:read"))):
    store = _store()
    alive: dict[str, bool] = {}
    try:
        from core.components.cluster.logic.cluster_service import cluster_service

        alive = {i.instance_id: bool(i.alive) for i in getattr(cluster_service, "_instances", [])}
    except Exception:
        pass
    items = store.instances()
    for item in items:
        item["alive"] = alive.get(item["instance_id"], False)
    return {
        "status": "ok",
        "self": _self_instance(),
        "enabled": store.enabled,
        "shipped": store.shipped,
        "dropped": store.dropped + store.handler.dropped,
        "instances": items,
    }
