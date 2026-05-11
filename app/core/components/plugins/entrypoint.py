from core.components.plugins.logic.models import ModuleManifest
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
        "subscribe": ["git:status_update", "system:boot_complete"],
        "emit": ["git:sync"],
    }
)

def setup(ctx):
    ctx.log.info("Plugin-System wird registriert...")
    register_plugin_routes()

    # Keep the local plugin-collection clone up to date via git-manager.
    ctx.subscribe("git:status_update")(plugin_service.on_git_status_update)
    # Delay the initial sync until all plugins (incl. git-manager) are active.
    ctx.subscribe("system:boot_complete")(plugin_service.on_boot_complete)
    plugin_service.start_collection_watcher()