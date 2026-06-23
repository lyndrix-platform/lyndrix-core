"""Helper for ordering API routes ahead of NiceGUI's root catch-all.

NiceGUI's ``ui.run_with(app)`` installs a catch-all page at ``path == ""`` that
otherwise shadows any ``/api/...`` route registered after it. Plugin routers and
plugin static mounts are added at runtime (after NiceGUI), so each must be spliced
to *before* the catch-all to be reachable.

This is a known brittleness (it depends on detecting the catch-all by its empty
path); centralizing it here means one place to log/guard and one place to fix if
NiceGUI's internals ever change. When NiceGUI is eventually retired there is no
catch-all to dodge and these calls become no-ops.
"""
from __future__ import annotations

from core.logger import get_logger

log = get_logger("Core:RouteOrder")


def move_routes_before_catchall(app, prefix: str) -> int:
    """Move every route whose path starts with ``prefix`` to just before the
    NiceGUI root catch-all (``path == ""``), preserving their relative order.

    Returns the number of routes moved. If no catch-all is found (NiceGUI absent
    or its structure changed) the routes are placed at the end and a warning is
    logged — they are appended in order, which is still correct when nothing
    shadows them.
    """
    routes = list(app.router.routes)
    target = [r for r in routes if getattr(r, "path", "").startswith(prefix)]
    if not target:
        return 0

    remaining = [r for r in routes if r not in target]
    root_idx = next(
        (i for i, r in enumerate(remaining) if getattr(r, "path", None) == ""),
        None,
    )
    if root_idx is None:
        log.warning(
            "Route reorder: NiceGUI catch-all (path='') not found; '%s' routes appended at end.",
            prefix,
        )
        root_idx = len(remaining)

    app.router.routes[:] = remaining[:root_idx] + target + remaining[root_idx:]
    app.openapi_schema = None
    log.debug("Route reorder: moved %d route(s) for '%s' before catch-all.", len(target), prefix)
    return len(target)
