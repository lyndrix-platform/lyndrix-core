"""
Lyndrix Core Plugin API — Stable Surface v1.1

Plugins SHOULD import everything they need from this package rather than
reaching into core.components.* internals. Breaking changes will bump
__api_version__.

Plugin contract
---------------
Every plugin entrypoint.py is encouraged to implement these functions:

    def setup(ctx: ModuleContext) -> None:
        \"\"\"Called when the plugin is activated.\"\"\"

    async def health(ctx: ModuleContext) -> PluginHealthStatus:
        \"\"\"
        Return the current health of this plugin.
        Absence is allowed but triggers a validation warning.
        \"\"\"
        return PluginHealthStatus(status="ok")

    def teardown(ctx: ModuleContext) -> None:
        \"\"\"Called when the plugin is deactivated.\"\"\"

Route registration
------------------
Plugins can expose HTTP endpoints without touching main.py::

    from fastapi import APIRouter
    from core.api import APIRouter  # re-exported for convenience

    router = APIRouter()

    @router.get("/status")
    def status():
        return {"ok": True}

    def setup(ctx):
        ctx.register_routes(router)
        # Routes are mounted under /api/plugins/<plugin-id>/
"""

__api_version__ = "1.1.0"

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field
from fastapi import APIRouter  # re-exported so plugins don't need a direct fastapi dep

from core.bus import bus as event_bus, GlobalEventBus
from core.logger import get_logger
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions
from core.components.plugins.logic.context import ModuleContext
from core.components.database.logic.db_service import db_instance, Base
from core.api.router_registry import router_registry
from core.api.security import (
    ApiIdentity,
    authenticate_request,
    optional_api_auth,
    require_api_auth,
    resolve_system_api_key,
    system_api_key_configured,
)
from version import __version__ as __core_version__


class PluginHealthStatus(BaseModel):
    """Typed return value for the plugin ``health()`` contract."""

    status: Literal["ok", "degraded", "error", "unknown"] = "unknown"
    details: Dict[str, Any] = Field(default_factory=dict)
    latency_ms: Optional[float] = None


# Lazy imports — avoids circular-import issues at module load time.
# Access these attributes after the application has fully booted.
def _lazy(service_path: str):
    """Return a lazy proxy that resolves the attribute on first access."""
    import importlib

    parts = service_path.rsplit(".", 1)
    mod_path, attr = parts[0], parts[1]

    class _Proxy:
        _resolved = None

        def __getattr__(self, name):
            if self._resolved is None:
                mod = importlib.import_module(mod_path)
                object.__setattr__(self, "_resolved", getattr(mod, attr))
            return getattr(self._resolved, name)

        def __repr__(self):
            return f"<LazyProxy for {service_path}>"

    return _Proxy()


notification_service = _lazy("core.components.notifications.notification_service.notification_service")
auth_service = _lazy("core.components.auth.logic.auth_service.auth_service")
monitor_service = _lazy("core.components.system.logic.monitor_service.monitor_service")
plugin_service = _lazy("core.components.plugins.logic.plugin_service.plugin_service")


__all__ = [
    "__api_version__",
    "__core_version__",
    # Event bus
    "event_bus",
    "GlobalEventBus",
    # Logging
    "get_logger",
    # Plugin models
    "ModuleManifest",
    "ModulePermissions",
    "ModuleContext",
    # Database
    "db_instance",
    "Base",
    # HTTP routing
    "APIRouter",
    "router_registry",
    # API authentication
    "ApiIdentity",
    "authenticate_request",
    "optional_api_auth",
    "require_api_auth",
    "resolve_system_api_key",
    "system_api_key_configured",
    # Health
    "PluginHealthStatus",
    # Core services (lazy)
    "notification_service",
    "auth_service",
    "monitor_service",
    "plugin_service",
]
