"""
Lyndrix Core Plugin API — Stable Surface v1.11

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

Runtime status (API >= 1.5): a plugin with a long warm-up or a maintenance
window reports it instead of letting its endpoints time out:

    ctx.set_status("starting", message="Initial probe sweep",
                   progress=(done, total), blocking=True)   # ... later:
    ctx.set_status("ready")
    async with ctx.maintenance("Re-importing inventory"): ...

While a *blocking* starting/maintenance status is set, the core answers the
plugin's API routes with 503 + Retry-After and the React shell shows a
warm-up panel; the status is broadcast as ``plugin:runtime_status``. Keep
health() cheap — it is polled by every dashboard.

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

Request/response over the bus (API 1.4)
---------------------------------------
The bus is fire-and-forget; ``ctx.request`` layers a correlated reply on top::

    reply = await ctx.request(
        "entity_registry:identify", {"identifiers": {...}},
        reply_topic="entity_registry:identified", timeout=5,
    )
    if reply is None:   # responder plugin absent/disabled → degrade, don't fail
        ...

The responder must echo ``payload["request_id"]`` in its reply. Declare the
request topic in ``permissions.emit`` and the reply topic in
``permissions.subscribe``.

Cluster (API 1.7, docs/ha/plugin-development.md)
------------------------------------------------
One code path for single and cluster mode::

    manifest = ModuleManifest(..., placement="any")   # any | singleton | pinned:<role>

    def setup(ctx):
        ctx.register_routes(router)                        # every instance
        ctx.on_lease("monitoring:prober",                  # exactly one instance
                     acquired=svc.start, lost=svc.stop)
        ctx.emit("monitoring:monitors_changed", {...}, cluster=True)   # reaches peers

    async with ctx.lease("itsm:nightly") as held: ...   # one-shot lease
    ctx.is_leader, ctx.instance_id, ctx.cluster_size, ctx.roles, ctx.cluster_mode
    ctx.cluster_instances, ctx.has_live_role("runner")   # API >= 1.7.1: who is alive with which roles
    ctx.cluster_workers("probe", count=2, roles=["runner"])   # API >= 1.8: load balancing — which
    ctx.my_shard("probe", keys, count=2, roles=["runner"])    # instances run a workload / my share
    ctx.release_lease("monitoring:prober")   # API >= 1.9: stop contending (fires ``lost`` when held)

``ctx.emit`` is thread-safe since core 1.7: a call from a worker thread
(``asyncio.to_thread`` code) is handed over to the application loop.

Navigation entries (API 1.9)
----------------------------
A plugin whose pages are data (one entry per embedded service, console, …)
publishes sidebar entries at runtime instead of manifest routes::

    ctx.register_nav_entries([
        {"id": "grafana", "label": "Grafana", "url": "https://grafana.example",
         "icon": "insights", "open_mode": "iframe", "group": "Services"},
    ])                     # replaces the plugin's previous set; [] removes all

Shells read ``GET /api/nav`` (filtered by the caller's ``plugin:<id>`` grant)
and refetch on ``nav:changed``; ``open_mode`` is ``iframe`` (embedded at
``/ext/<plugin>/<id>``), ``new_tab`` or ``route`` (API 1.10: an in-shell path
such as ``/apps/<plugin>/<route>``). Entries are per instance — register them
from the shared DB on every instance — and vanish with the plugin.

Panels (API 1.11)
-----------------
A plugin declares floating, per-user pinnable windows that the React shell
hosts above every page (launcher in the header, position/size/open state
remembered per user in ``/api/me/preferences`` under ``panels``)::

    manifest = ModuleManifest(..., react_ui=True, panels=[
        {"id": "chat", "label": "Assistant", "icon": "smart_toy",
         "description": "Ask the AI about this installation",
         "default_size": {"width": 420, "height": 560},
         "permission": "plugin:lyndrix.plugin.ai_assistant:api:read"},   # optional
    ])

The shell loads the plugin's own React bundle into the window and renders
``<PluginApp panel={{ id: "chat", pluginId: "<manifest.id>" }} />``. A plugin
that ignores the ``panel`` prop shows its default view; a panel-aware one
switches to a compact layout. ``panels`` is part of ``PluginOut`` (``GET
/api/plugins``); ids are slugs (``[a-z][a-z0-9_-]{0,40}``).

``ModuleManifest.ai_instructions`` (API 1.11) is Markdown a plugin hands to
the AI assistant about itself (purpose, API paths, vocabulary, cautions; max
20 kB). It is NOT served to shells.

module_registry (API 1.11)
--------------------------
A read-only view of the loaded manifests for plugins that need to know about
other plugins (the AI assistant collects every ``ai_instructions``)::

    from core.api import module_registry
    for m in module_registry.get_manifests():      # List[ModuleManifest]
        ...
    module_registry.get("lyndrix.plugin.itsm")     # ModuleManifest | None
    module_registry.is_active("lyndrix.plugin.itsm")

It is a view, not the manager: no loading, enabling or registry mutation.
Treat the data as optional — another plugin may be absent or disabled.

Old cores: guard with ``getattr(ctx, "on_lease", None)`` and start the work
directly when it is missing.
"""

__api_version__ = "1.12.0"

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field
from fastapi import APIRouter  # re-exported so plugins don't need a direct fastapi dep

from core.bus import bus as event_bus, GlobalEventBus
from core.logger import get_logger
from core.components.plugins.logic.models import (
    ModuleManifest,
    ModulePermissions,
    NotificationEndpoint,
    PluginSettingField,
)
from core.components.plugins.logic.context import ModuleContext
from core.components.database.logic.db_service import db_instance, Base
from core.api.router_registry import router_registry
from core.api.security import (
    ApiIdentity,
    authenticate_request,
    optional_api_auth,
    require_api_auth,
    require_permission,
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


notification_service = _lazy("core.components.notifications.logic.notification_service.notification_service")
auth_service = _lazy("core.components.auth.logic.auth_service.auth_service")
monitor_service = _lazy("core.components.system.logic.monitor_service.monitor_service")
plugin_service = _lazy("core.components.plugins.logic.plugin_service.plugin_service")
# Read-only cluster view (instances, leader, leases). Plugins normally use the
# ctx.* accessors; this is for dashboards/diagnostics.
cluster_service = _lazy("core.components.cluster.logic.cluster_service.cluster_service")


class _ModuleRegistryView:
    """Read-only window onto the module manager's registry (API 1.11).

    Deliberately NOT the whole ``ModuleManager``: plugins get the loaded
    manifests and activation state, nothing that loads, enables or mutates
    modules. Resolves the manager lazily (circular imports at module load).
    """

    @staticmethod
    def _manager():
        from core.components.plugins.logic.manager import module_manager

        return module_manager

    def get_manifests(self) -> list:
        """Manifests of every loaded module (core components and plugins)."""
        return list(self._manager().get_manifests())

    def get(self, module_id: str):
        """Manifest of one loaded module, or ``None`` when it is not loaded."""
        entry = self._manager().registry.get(module_id)
        return entry.get("manifest") if entry else None

    def is_active(self, module_id: str) -> bool:
        """Whether the module is loaded *and* active (not disabled/failed/remote)."""
        entry = self._manager().registry.get(module_id)
        return bool(entry) and entry.get("status") == "active"

    def status(self, module_id: str):
        """Registry status of a loaded module (``active``, ``remote``, ``disabled``,
        ``failed``, ...) or ``None`` when it is not loaded (API 1.12)."""
        entry = self._manager().registry.get(module_id)
        return entry.get("status") if entry else None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<ModuleRegistryView (read-only)>"


module_registry = _ModuleRegistryView()

# Theming — stable plugin surface
# Import after services to avoid circular-import issues at module load time.
from ui.theme import UIStyles  # noqa: E402
from core.theming import get_theme_engine  # noqa: E402
from core.theming.engine import ThemeEngine  # noqa: E402
from core.theming.models import ThemePack, ThemeTokens, ThemeComponents  # noqa: E402
from core.theming.loader import load_theme_pack  # noqa: E402

# Messaging Gateway — stable plugin surface
from core.components.messaging.logic.adapter import GatewayAdapter, GatewayCapability, ProviderConfigField  # noqa: E402
from core.components.messaging.model.schemas import (  # noqa: E402
    OutboundMessage,
    InboundMessage,
    ActionButton,
    MessageSeverity,
    DeliveryResult,
)
from core.components.messaging.logic.correlation import CorrelationStore, PendingAction  # noqa: E402
from core.components.messaging.logic.gateway import messaging_gateway, StreamBridge  # noqa: E402

# Notification routing — stable plugin surface
from core.components.notification_router.models import (  # noqa: E402
    NotificationEnvelope,
    ResolvedState,
)


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
    "NotificationEndpoint",
    "PluginSettingField",
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
    "require_permission",
    "resolve_system_api_key",
    "system_api_key_configured",
    # Health
    "PluginHealthStatus",
    # Core services (lazy)
    "notification_service",
    "auth_service",
    "monitor_service",
    "plugin_service",
    "cluster_service",
    # Read-only manifests of loaded modules (API 1.11)
    "module_registry",
    # Theming
    "UIStyles",
    "ThemeEngine",
    "get_theme_engine",
    "ThemePack",
    "ThemeTokens",
    "ThemeComponents",
    "load_theme_pack",
    # Messaging Gateway
    "GatewayAdapter",
    "GatewayCapability",
    "ProviderConfigField",
    "OutboundMessage",
    "InboundMessage",
    "ActionButton",
    "MessageSeverity",
    "DeliveryResult",
    "CorrelationStore",
    "PendingAction",
    "messaging_gateway",
    "StreamBridge",
    # Notification routing
    "NotificationEnvelope",
    "ResolvedState",
]
