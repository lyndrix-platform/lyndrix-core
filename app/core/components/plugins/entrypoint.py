from core.components.plugins.logic.models import ModuleManifest, NotificationEndpoint
from .ui.routes import register_plugin_routes
from .logic.plugin_service import plugin_service

manifest = ModuleManifest(
    id="lyndrix.core.plugins",
    name="Plugin Manager",
    version="1.0.0",
    description="Verwaltung von System-Erweiterungen.",
    author="Lyndrix",
    icon="extension",
    ui_route="/plugins",
    type="CORE",
    permissions={
        "subscribe": [
            "git:status_update", "system:boot_complete",
            "plugin:installed", "plugin:install_failed", "plugin:rolled_back",
            "plugin:update_available",
        ],
        "emit": ["git:sync", "notification:routed"],
    },
    # Durable, operator-routable lifecycle notifications. internal_toast is off
    # because the Plugins UI already raises a live toast for the active client;
    # internal_persist keeps a record in the in-app bell that survives the client
    # tearing down during the (slow) install. Failures/rollbacks also route to the
    # external default provider (e.g. Discord) when one is configured.
    notification_endpoints=[
        NotificationEndpoint(
            name="plugin_installed",
            description="A plugin was installed or upgraded successfully.",
            internal_toast=False, internal_persist=True, external_default=False,
        ),
        NotificationEndpoint(
            name="plugin_install_failed",
            description="A plugin install or upgrade failed.",
            internal_toast=False, internal_persist=True, external_default=True,
        ),
        NotificationEndpoint(
            name="plugin_rolled_back",
            description="A plugin upgrade was reverted to the previous version.",
            internal_toast=False, internal_persist=True, external_default=True,
        ),
        NotificationEndpoint(
            name="plugin_update_available",
            description="A newer release tag exists for an installed plugin.",
            internal_toast=False, internal_persist=True, external_default=False,
        ),
    ],
)


def _register_lifecycle_notifications(ctx):
    """Emit durable, routed notifications on plugin lifecycle bus events.

    Server-side (not tied to the originating browser), so the record survives the
    UI client disconnecting during a long install/upgrade — the gap that made
    install confirmations silently vanish ("skipped notify after client teardown")."""

    def _on_installed(payload):
        repo = (payload or {}).get("repo", "plugin")
        ctx.notify(
            "plugin_installed",
            payload=payload or {},
            notification_id=f"plugin_{repo}",
            title="Plugin installed",
            body=f"'{repo}' was installed/updated successfully.",
            severity="success",
        )

    def _on_failed(payload):
        repo = (payload or {}).get("repo", "plugin")
        error = (payload or {}).get("error", "unknown error")
        ctx.notify(
            "plugin_install_failed",
            payload=payload or {},
            notification_id=f"plugin_{repo}",
            title="Plugin install failed",
            body=f"'{repo}' failed: {error}",
            severity="error",
        )

    def _on_rolled_back(payload):
        repo = (payload or {}).get("repo", "plugin")
        version = (payload or {}).get("version")
        ctx.notify(
            "plugin_rolled_back",
            payload=payload or {},
            notification_id=f"plugin_{repo}",
            title="Plugin reverted",
            body=f"'{repo}' was reverted to the previous version"
                 + (f" ({version})." if version else "."),
            severity="warning",
        )

    def _on_update_available(payload):
        pid = (payload or {}).get("plugin_id", "plugin")
        latest = (payload or {}).get("latest_version", "?")
        current = (payload or {}).get("current_version", "?")
        ctx.notify(
            "plugin_update_available",
            payload=payload or {},
            # Stable id per plugin: a newer tag replaces the old bell entry.
            notification_id=f"plugin_update_{pid}",
            title="Plugin update available",
            body=f"'{pid}': {current} → {latest}",
            severity="info",
        )

    ctx.subscribe("plugin:installed")(_on_installed)
    ctx.subscribe("plugin:install_failed")(_on_failed)
    ctx.subscribe("plugin:rolled_back")(_on_rolled_back)
    ctx.subscribe("plugin:update_available")(_on_update_available)


def setup(ctx):
    ctx.log.info("Plugin-System wird registriert...")
    register_plugin_routes()

    # Keep the local plugin-collection clone up to date via git-manager.
    ctx.subscribe("git:status_update")(plugin_service.on_git_status_update)
    # Delay the initial sync until all plugins (incl. git-manager) are active.
    ctx.subscribe("system:boot_complete")(plugin_service.on_boot_complete)
    plugin_service.start_collection_watcher()
    plugin_service.start_update_checker()

    # Durable, gateway-routed lifecycle notifications.
    _register_lifecycle_notifications(ctx)