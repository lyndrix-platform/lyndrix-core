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
            "plugin:update_available", "plugin:uninstall_failed",
            "plugin:operation_finished",
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
            required_permission="feature:plugins.manage",
        ),
        NotificationEndpoint(
            name="plugin_install_failed",
            description="A plugin install or upgrade failed.",
            internal_toast=False, internal_persist=True, external_default=True,
            required_permission="feature:plugins.manage",
        ),
        NotificationEndpoint(
            name="plugin_rolled_back",
            description="A plugin upgrade was reverted to the previous version.",
            internal_toast=False, internal_persist=True, external_default=True,
            required_permission="feature:plugins.manage",
        ),
        NotificationEndpoint(
            name="plugin_update_available",
            description="A newer release tag exists for an installed plugin.",
            internal_toast=False, internal_persist=True, external_default=False,
            required_permission="feature:plugins.manage",
        ),
    ],
)


def _register_lifecycle_notifications(ctx):
    """Emit durable, routed notifications on plugin lifecycle bus events.

    Server-side (not tied to the originating browser), so the record survives the
    UI client disconnecting during a long install/upgrade — the gap that made
    install confirmations silently vanish ("skipped notify after client teardown")."""

    def _tracked(repo: str) -> bool:
        """A cluster-wide operation (core 1.8) tracks this repo: the per-event
        notification would be a duplicate of the one the operation produces."""
        try:
            from .logic.operations import operation_tracker

            return operation_tracker.has_running(repo_name=str(repo).replace("-", "_"))
        except Exception:
            return False

    def _on_operation_finished(payload):
        p = payload or {}
        name = p.get("plugin_id") or p.get("repo_name") or "plugin"
        version = p.get("version") or "latest"
        action = p.get("action") or "install"
        done, total = int(p.get("done") or 0), int(p.get("total") or 0)
        failed = p.get("failed") or []
        pending = p.get("pending") or []
        if p.get("status") == "done":
            ctx.notify(
                "plugin_installed",
                payload=p,
                notification_id=f"plugin_{name}",
                title="Plugin installed" if action == "install" else "Plugin updated",
                body=f"'{name}' {action} → {version} on {done}/{total} instance(s).",
                severity="success",
            )
            return
        parts = [f"'{name}' {action} → {version}: {done}/{total} instance(s) ok."]
        if failed:
            parts.append("Failed: " + ", ".join(
                f"{f.get('instance_id')} ({f.get('status')}{': ' + str(f.get('message'))[:80] if f.get('message') else ''})"
                for f in failed
            ))
        if pending:
            parts.append("No report from: " + ", ".join(pending))
        ctx.notify(
            "plugin_install_failed",
            payload=p,
            notification_id=f"plugin_{name}",
            title="Plugin update incomplete" if done else "Plugin install failed",
            body=" ".join(parts),
            severity="warning" if done else "error",
        )

    def _on_installed(payload):
        repo = (payload or {}).get("repo", "plugin")
        if _tracked(repo):
            return
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
        if _tracked(repo):
            return
        ctx.notify(
            "plugin_install_failed",
            payload=payload or {},
            notification_id=f"plugin_{repo}",
            title="Plugin install failed",
            body=f"'{repo}' failed: {error}",
            severity="error",
        )

    def _on_uninstall_failed(payload):
        # Cluster: the plugin_states row survived the uninstall, so the other
        # instances keep the plugin. Loud on purpose — a revocation that fails
        # must never fail silently.
        pid = (payload or {}).get("plugin_id", "plugin")
        error = (payload or {}).get("error", "unknown error")
        ctx.notify(
            "plugin_install_failed",
            payload=payload or {},
            notification_id=f"plugin_uninstall_{pid}",
            title="Plugin uninstall incomplete",
            body=f"'{pid}': {error}",
            severity="error",
        )

    def _on_rolled_back(payload):
        repo = (payload or {}).get("repo", "plugin")
        version = (payload or {}).get("version")
        if _tracked(repo):
            return
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

    def _local_only(handler):
        # Cluster: these topics are bridged, so a peer's event arrives here with
        # payload["_origin"]. The peer already created the notification and
        # notification:new is bridged too — creating it again here would double
        # (×N instances) every toast and bell entry.
        def _guarded(payload):
            if (payload or {}).get("_origin"):
                return
            return handler(payload)

        _guarded.__qualname__ = f"{handler.__qualname__}<local_only>"
        _guarded.__module__ = handler.__module__
        return _guarded

    ctx.subscribe("plugin:operation_finished")(_local_only(_on_operation_finished))
    ctx.subscribe("plugin:installed")(_local_only(_on_installed))
    ctx.subscribe("plugin:install_failed")(_local_only(_on_failed))
    ctx.subscribe("plugin:uninstall_failed")(_local_only(_on_uninstall_failed))
    ctx.subscribe("plugin:rolled_back")(_local_only(_on_rolled_back))
    ctx.subscribe("plugin:update_available")(_local_only(_on_update_available))


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

    # Cluster-wide operations: participants that never report are closed by
    # the leader after a timeout so an operation can never hang "running".
    async def _sweep_operations():
        import asyncio

        from .logic.operations import operation_tracker

        while True:
            await asyncio.sleep(60)
            try:
                if getattr(ctx, "is_leader", True) and operation_tracker.available():
                    await asyncio.to_thread(operation_tracker.sweep_timeouts)
            except Exception as exc:
                ctx.log.debug(f"OPS: sweep failed: {exc}")

    ctx.create_task(_sweep_operations(), name="plugins:operation_sweep")