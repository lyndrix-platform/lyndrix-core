import os
import sys
import importlib
from pathlib import Path
import inspect
import asyncio
from typing import List
from sqlalchemy import inspect as sqlalchemy_inspect, text
from packaging.version import Version, InvalidVersion
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from core.logger import get_logger
from core.bus import bus
from core.components.database.logic.db_service import db_instance
from .models import ModuleManifest, PluginState, PluginVersionHistory
from .context import ModuleContext
from config import settings
from core.i18n import register_plugin_locales

log = get_logger("Core:ModuleManager")

class ModuleManager:
    def __init__(self):
        self.registry = {}
        self._plugin_state_schema_ready = False
        current_file_path = os.path.abspath(__file__)
        self.base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_file_path)))))
        # Subscribe to filesystem change events from the PluginService
        bus.subscribe("plugin:files_changed")(self._handle_plugin_change)

    def _find_plugin_id_by_folder(self, module_name: str):
        """Resolve an already-loaded plugin id from its folder/package name."""
        for module_id, entry in self.registry.items():
            manifest = entry.get("manifest")
            module = entry.get("module")
            if not manifest or manifest.type != "PLUGIN" or not module:
                continue

            try:
                folder_name = Path(module.__file__).parent.name
            except Exception:
                folder_name = module.__name__.split('.')[-2]

            if folder_name == module_name:
                return module_id
        return None

    def _read_plugin_manifest_id(self, module_name: str):
        """Statically read manifest.id from a plugin folder on disk (no import).

        Used to resolve an already-loaded plugin during an in-place upgrade when
        folder-name resolution fails — e.g. the on-disk folder was atomically
        swapped or the folder name changed between versions. Reuses the AST-based
        extractor on PluginService (imported lazily to avoid an import cycle)."""
        try:
            from core.components.plugins.logic.plugin_service import PluginService
            entrypoint = Path(self.base_path) / "plugins" / module_name / "entrypoint.py"
            if entrypoint.exists():
                return PluginService._extract_manifest_id(str(entrypoint))
        except Exception as exc:
            log.debug(f"MANAGER: manifest-id probe failed for '{module_name}': {exc}")
        return None


    def load_all(self):
        """Scans directories and loads all valid core components and plugins."""
        log.info(f"STARTUP: Initiating unified module discovery process in {self.base_path}")
        
        if self.base_path not in sys.path:
            sys.path.insert(0, self.base_path)
        
        # 1. Load System Components
        core_dir = os.path.join(self.base_path, "core", "components")
        self._scan_directory("core.components", core_dir, is_plugin=False)
        
        # 2. Load User Plugins
        plugin_dir = os.path.join(self.base_path, "plugins")
        self._scan_directory("plugins", plugin_dir, is_plugin=True)
        
        log.info(f"SUCCESS: Boot sequence finished. {len(self.registry)} modules registered")
        
        # --- THE CRITICAL TIMING FIX ---
        # Now that the registry is fully populated, we can safely read the DB states.
        if db_instance.is_connected:
            bus.create_tracked_task(
                self._activate_saved_plugins(),
                name="module_manager:activate_plugins"
            )
        else:
            log.warning("PLUGIN_MANAGER: DB not connected at end of load_all. Falling back to bus listener.")
            bus.subscribe("db:connected")(self._activate_saved_plugins)

    def _scan_directory(self, package_prefix: str, directory: str, is_plugin: bool):
        if not os.path.exists(directory):
            log.warning(f"SYSTEM: Directory not found: {directory}")
            return

        for item in os.listdir(directory):
            if item.startswith("__") or item.startswith("."):
                continue
            
            item_path = os.path.join(directory, item)
            
            if os.path.isdir(item_path):
                entrypoint_path = os.path.join(item_path, "entrypoint.py")
                
                # --- VENDOR CHECK: Validate vendor directory exists ---
                # Dependencies must be installed during plugin install/build time,
                # NOT at boot. We only verify the vendor dir is present.
                if is_plugin:
                    req_file = Path(item_path) / "requirements.txt"
                    vendor_dir = Path(item_path) / "vendor"

                    if req_file.exists() and not vendor_dir.exists():
                        log.warning(
                            f"VENDORS: Plugin '{item}' has requirements.txt but no vendor directory. "
                            f"Dependencies may be missing. Re-install the plugin to resolve."
                        )

                if not os.path.exists(entrypoint_path):
                    continue
                
                # Auto-fix dashes to underscores for Python imports
                if "-" in item:
                    new_name = item.replace("-", "_")
                    new_path = os.path.join(directory, new_name)
                    log.warning(f"AUTO-FIX: Renaming '{item}' to '{new_name}' to allow Python import.")
                    os.rename(item_path, new_path)
                    item = new_name 

                if not item.isidentifier():
                    log.error(f"SCAN_ERROR: Folder '{item}' has an invalid name! Only a-z, 0-9 and _ allowed.")
                    continue

                self.load_module(item, is_plugin=is_plugin)

    def load_module(self, module_name: str, is_plugin: bool = True):
        prefix = "plugins" if is_plugin else "core.components"
        full_module_path = f"{prefix}.{module_name}.entrypoint"
        importlib.invalidate_caches()
        
        # --- VENDORING: Add plugin's private dependency folder to the Python path ---
        vendor_path_str = None
        path_added = False
        if is_plugin:
            plugin_dir_path = Path(self.base_path) / prefix.replace('.', '/') / module_name
            vendor_path = plugin_dir_path / "vendor"
            if vendor_path.exists() and vendor_path.is_dir():
                vendor_path_str = str(vendor_path)
                if vendor_path_str not in sys.path:
                    log.debug(f"VENDORS: Adding {vendor_path_str} to sys.path for '{module_name}'")
                    # Append (not insert at 0) so vendored packages never shadow core packages.
                    sys.path.append(vendor_path_str)
                    path_added = True
        
        try:
            module = importlib.import_module(full_module_path)
            
            if not hasattr(module, 'manifest'):
                log.warning(f"VALIDATION_ERROR: Module '{module_name}' missing 'manifest'")
                return False

            raw_manifest = module.manifest
            manifest = ModuleManifest(**raw_manifest) if isinstance(raw_manifest, dict) else raw_manifest

            # Validate manifest constraints
            validation_issues: List[str] = []
            critical_issues: List[str] = []
            if is_plugin:
                validation_issues, critical_issues = self._validate_manifest(manifest)
                if validation_issues:
                    log.warning(
                        f"VALIDATION: Plugin '{module_name}' has issues: "
                        f"{'; '.join(validation_issues)}"
                    )

            if manifest.id in self.registry:
                # An already-registered id reached the fresh-load path. Upgrades
                # should route through reload_module (see _handle_plugin_change);
                # log distinctly so this never resurfaces as a generic
                # "could not be loaded" failure.
                log.warning(
                    f"ALREADY_REGISTERED: '{manifest.id}' is already loaded; skipping "
                    f"duplicate load of folder '{module_name}'."
                )
                return False

            # Eagerly register declared notification endpoints so plugins can
            # call ctx.notify(...) during setup() before the router runs its
            # db:connected sync pass. The router's later sync is idempotent.
            if getattr(manifest, "notification_endpoints", None):
                try:
                    from core.components.notification_router.logic.endpoint_registry import (
                        endpoint_registry,
                    )
                    for ep in manifest.notification_endpoints:
                        endpoint_registry.upsert(manifest.id, ep)
                except Exception as exc:
                    log.debug(
                        f"NOTIFY: deferred endpoint registration for '{manifest.id}' "
                        f"(router not ready): {exc}"
                    )

            ctx = ModuleContext(manifest)

            # Register plugin locale files so t() works with this plugin's namespace.
            if is_plugin:
                plugin_dir_path = Path(self.base_path) / "plugins" / module_name
                register_plugin_locales(plugin_dir_path)

            # Determine initial status. Only CRITICAL issues (incompatible core
            # version, missing/incompatible dependencies) mark a plugin 'degraded'
            # and block setup. Non-critical issues (missing description/icon, or a
            # missing optional health() function) are warn-only and must NOT
            # prevent activation — they are already logged above.
            initial_status = "initializing"
            if is_plugin and critical_issues:
                initial_status = "degraded"

            # Register as initializing/parked/degraded
            self.registry[manifest.id] = {
                "manifest": manifest,
                "module": module,
                "context": ctx,
                "status": initial_status
            }

            if not is_plugin:
                # Core modules boot instantly without waiting for the DB
                self._execute_setup(manifest.id)
                log.info(f"LOAD_SUCCESS: CORE '{manifest.name}' is online")
            else:
                if critical_issues:
                    log.error(
                        f"LOAD_DEGRADED: PLUGIN '{manifest.name}' loaded but blocked "
                        f"from activation due to critical issues: {'; '.join(critical_issues)}"
                    )
                else:
                    # Plugins wait for the DB event
                    log.info(f"LOAD_PENDING: PLUGIN '{manifest.name}' loaded, awaiting DB state...")

            return True

        except Exception as e:
            log.error(f"LOAD_ERROR: Failed to load '{module_name}': {e}")
            # --- VENDORING: Clean up sys.path on a failed import ---
            if path_added and vendor_path_str in sys.path:
                log.debug(f"VENDORS: Cleaning up failed import path {vendor_path_str}")
                sys.path.remove(vendor_path_str)
            return False

    async def _handle_plugin_change(self, payload: dict):
        """Reacts to install/uninstall events from the PluginService."""
        action = payload.get("action")
        if action == "install":
            module_name = payload.get("name")
            log.info(f"MANAGER: Received install event for '{module_name}'. Loading module...")
            existing_module_id = self._find_plugin_id_by_folder(module_name)
            if not existing_module_id:
                # Folder-name resolution can miss when an upgrade atomically swapped
                # the on-disk folder or the folder name changed between versions.
                # Fall back to resolving by the manifest id declared in the freshly
                # installed files so we reload in place (which purges sys.modules and
                # re-imports the NEW code) instead of hitting the already-registered
                # guard in load_module and failing with a misleading error.
                probed_id = self._read_plugin_manifest_id(module_name)
                if probed_id and probed_id in self.registry:
                    existing_module_id = probed_id
                    log.info(
                        f"MANAGER: Resolved already-loaded plugin '{module_name}' by manifest id "
                        f"'{probed_id}' (folder lookup missed)."
                    )
            if existing_module_id:
                log.info(
                    f"MANAGER: Plugin folder '{module_name}' is already loaded as '{existing_module_id}'. Reloading in-place."
                )
                load_ok = await self.reload_module(existing_module_id, module_folder=module_name)
            else:
                load_ok = self.load_module(module_name, is_plugin=True)
                if not load_ok:
                    log.warning(
                        f"MANAGER: Plugin '{module_name}' could not be loaded after install event."
                    )

            resolved_module_id = (
                self._find_plugin_id_by_folder(module_name)
                or self._read_plugin_manifest_id(module_name)
            )

            # Persist + activate (runs setup) before the health gate.
            if load_ok and resolved_module_id:
                self._persist_plugin_state(resolved_module_id, True)
                await self._activate_saved_plugins()

            # Verification transaction: load + setup + health() must all pass; on
            # failure auto-revert to the retained previous version (upgrades only).
            verified = bool(
                load_ok
                and resolved_module_id
                and await self._verify_after_install(resolved_module_id)
            )
            if not verified:
                await self._auto_revert(module_name, resolved_module_id)
        elif action == "uninstall":
            module_id = payload.get("id")
            self.unload_module(module_id)

    async def _verify_after_install(self, module_id: str) -> bool:
        """Post-install/upgrade health gate. The plugin must be loaded, not in a
        failed/degraded state, and — if it implements one — its ``health(ctx)``
        must not report ``error``. A plugin with no health() passes on a clean
        load+setup."""
        entry = self.registry.get(module_id)
        if not entry:
            log.warning(f"VERIFY: '{module_id}' is not loaded after install.")
            return False
        status = entry.get("status")
        if status in ("failed", "degraded"):
            log.warning(f"VERIFY: '{module_id}' is '{status}' after install.")
            return False
        health_fn = getattr(entry.get("module"), "health", None)
        if not callable(health_fn):
            return True
        try:
            result = health_fn(entry.get("context"))
            if inspect.isawaitable(result):
                result = await result
            if getattr(result, "status", "ok") == "error":
                log.warning(
                    f"VERIFY: health() reported 'error' for '{module_id}': "
                    f"{getattr(result, 'details', {})}"
                )
                return False
            return True
        except Exception as exc:
            log.error(f"VERIFY: health() raised for '{module_id}': {exc}", exc_info=True)
            return False

    async def _auto_revert(self, module_name: str, module_id):
        """Restore the retained previous version after a failed upgrade
        verification. No-op for fresh installs (nothing to revert to)."""
        from core.components.plugins.logic.plugin_service import plugin_service
        if not plugin_service.has_previous(module_name):
            log.error(
                f"AUTO-REVERT: verification failed for '{module_name}' and no previous version "
                f"is retained; leaving it in place for manual intervention."
            )
            return
        log.warning(
            f"AUTO-REVERT: verification failed for '{module_name}'; restoring previous version."
        )
        prev_version = plugin_service.previous_version(module_name)
        # Drop the broken version from memory before swapping files back.
        if module_id and module_id in self.registry:
            self.unload_module(module_id)
        if not plugin_service.restore_previous(module_name):
            log.error(f"AUTO-REVERT: restore failed for '{module_name}'.")
            return
        importlib.invalidate_caches()
        self.load_module(module_name, is_plugin=True)
        restored_id = self._find_plugin_id_by_folder(module_name) or module_id
        if restored_id and db_instance.is_connected:
            await self._activate_saved_plugins()
        plugin_service._record_history(
            restored_id, module_name, prev_version, action="upgrade", outcome="reverted",
        )
        bus.emit("plugin:rolled_back",
                 {"repo": module_name, "module_id": restored_id, "version": prev_version})
        bus.emit("plugin:install_failed",
                 {"repo": module_name, "error": "verification_failed_auto_reverted"})
        bus.emit("ui:needs_refresh", {"reason": f"Plugin {module_name} auto-reverted."})
        log.info(f"AUTO-REVERT: '{module_name}' restored to previous version {prev_version}.")

    def _execute_setup(self, module_id):
        """Helper to safely execute the module's setup function."""
        entry = self.registry.get(module_id)
        if not entry:
            return

        if entry.get("status") == "active":
            return

        # Degraded plugins (critical manifest issues) must not be activated.
        if entry.get("status") == "degraded":
            log.warning(
                f"SETUP_BLOCKED: Module '{module_id}' is degraded; skipping setup()."
            )
            return

        module = entry["module"]
        ctx = entry["context"]

        if hasattr(module, 'setup'):
            if inspect.iscoroutinefunction(module.setup):
                bus.create_tracked_task(
                    self._safe_async_setup(module, ctx, module_id),
                    name=f"module_setup:{module_id}"
                )
            else:
                try:
                    module.setup(ctx)
                    entry["status"] = "active"
                except Exception as e:
                    log.error(f"RUNTIME_ERROR: Sync setup failed for '{module_id}': {e}")
                    entry["status"] = "failed"
                    try:
                        bus.emit("plugin:setup_failed", {"plugin_id": module_id, "error": str(e)})
                    except Exception as emit_err:
                        log.error(f"BUS_ERROR: Failed to emit plugin:setup_failed for '{module_id}': {emit_err}")

    def _persist_plugin_state(self, module_id: str, is_active: bool):
        """Persist plugin activation without duplicating lifecycle work."""
        if module_id not in self.registry or not db_instance.SessionLocal:
            return

        self._ensure_plugin_state_schema()
        manifest = self.registry[module_id]["manifest"]

        try:
            with db_instance.SessionLocal() as session:
                db_state = session.query(PluginState).filter(PluginState.module_id == module_id).first()
                if not db_state:
                    db_state = PluginState(
                        module_id=module_id,
                        is_active=is_active,
                        installed_version=manifest.version,
                        desired_version=manifest.version,
                        repo_url=manifest.repo_url,
                        auto_update=settings.LYNDRIX_PLUGINS_AUTO_UPDATE,
                    )
                    session.add(db_state)
                else:
                    db_state.is_active = is_active
                    db_state.installed_version = manifest.version
                    db_state.desired_version = manifest.version
                    db_state.repo_url = manifest.repo_url or db_state.repo_url
                session.commit()
        except Exception as e:
            log.error(f"DB_ERROR: Failed to persist plugin state for '{module_id}': {e}")

    async def _safe_async_setup(self, module, ctx, module_id):
        try:
            await module.setup(ctx)
            if module_id in self.registry:
                self.registry[module_id]["status"] = "active"
        except Exception as e:
            log.error(f"RUNTIME_ERROR: Async setup failed for '{module_id}': {e}")
            if module_id in self.registry:
                self.registry[module_id]["status"] = "failed"
            # Emit on the global bus directly (bypass plugin permissions in ctx).
            try:
                bus.emit("plugin:setup_failed", {"plugin_id": module_id, "error": str(e)})
            except Exception as emit_err:
                log.error(f"BUS_ERROR: Failed to emit plugin:setup_failed for '{module_id}': {emit_err}")

    async def _activate_saved_plugins(self, payload=None):
        """Called when DB connects. Reads states and boots active plugins."""
        log.info("PLUGIN_MANAGER: Verifying plugin states from Database...")

        if not db_instance.SessionLocal:
            log.error("PLUGIN_MANAGER: SessionLocal missing during DB activation.")
            return

        self._ensure_plugin_state_schema()

        # Custom plugin repositories live in their own table; create it on the
        # same db:connected pass so the marketplace can read it immediately.
        try:
            from .custom_repo_service import custom_repo_service
            custom_repo_service.ensure_schema()
        except Exception as exc:
            log.warning(f"PLUGIN_MANAGER: custom repo schema bootstrap failed: {exc}")

        enabled_plugin_ids = []

        with db_instance.SessionLocal() as session:
            for module_id, entry in list(self.registry.items()):
                if entry["manifest"].type == "CORE":
                    continue

                manifest = entry["manifest"]
                db_state = session.query(PluginState).filter(PluginState.module_id == module_id).first()

                if not db_state:
                    db_state = PluginState(
                        module_id=module_id,
                        is_active=manifest.auto_enable_on_install,
                        installed_version=manifest.version,
                        desired_version=manifest.version,
                        repo_url=manifest.repo_url,
                        auto_update=settings.LYNDRIX_PLUGINS_AUTO_UPDATE,
                    )
                    session.add(db_state)
                else:
                    db_state.installed_version = manifest.version
                    db_state.repo_url = manifest.repo_url or db_state.repo_url
                    if not db_state.desired_version:
                        db_state.desired_version = manifest.version

                if db_state.is_active:
                    enabled_plugin_ids.append(module_id)
                    if not self._check_dependencies_met(manifest):
                        entry["status"] = "blocked"
                        log.warning(
                            f"DB_RESTORE: Plugin '{module_id}' is enabled but waiting for dependencies."
                        )
                    elif entry.get("status") != "active":
                        log.info(f"DB_RESTORE: Activating plugin '{module_id}'")
                        self._execute_setup(module_id)
                else:
                    if entry.get("status") != "disabled":
                        log.info(f"DB_RESTORE: Plugin '{module_id}' remains disabled.")
                        entry["status"] = "disabled"

            session.commit()

        pending_enabled = set(enabled_plugin_ids)
        while pending_enabled:
            progressed = False
            for module_id in list(pending_enabled):
                entry = self.registry.get(module_id)
                if not entry or entry.get("status") == "active":
                    pending_enabled.discard(module_id)
                    continue

                manifest = entry["manifest"]
                if not self._check_dependencies_met(manifest):
                    entry["status"] = "blocked"
                    continue

                log.info(f"DB_RESTORE: Activating dependency-ready plugin '{module_id}'")
                entry["status"] = "initializing"
                self._execute_setup(module_id)
                pending_enabled.discard(module_id)
                progressed = True

            if not progressed:
                break

        # After restoring known plugins, reconcile desired plugins from config
        await self.reconcile_desired_plugins()

    def toggle_module(self, module_id: str, active: bool):
        """Toggles the module state in RAM and persists it to the DB."""
        if module_id not in self.registry:
            return False

        self._ensure_plugin_state_schema()
            
        # 1. Persist to DB
        if db_instance.SessionLocal:
            try:
                with db_instance.SessionLocal() as session:
                    db_state = session.query(PluginState).filter(PluginState.module_id == module_id).first()
                    if not db_state:
                        db_state = PluginState(module_id=module_id, is_active=active)
                        session.add(db_state)
                    else:
                        db_state.is_active = active
                    session.commit()
            except Exception as e:
                log.error(f"DB_ERROR: Failed to save toggle state: {e}")
                return False

        # 2. Update RAM status and execute lifecycle hooks
        entry = self.registry[module_id]

        if active:
            manifest = entry["manifest"]
            if not self._check_dependencies_met(manifest):
                entry["status"] = "blocked"
                log.warning(
                    f"MODULE: Plugin '{module_id}' was enabled but is waiting for dependencies."
                )
            else:
                entry["status"] = "initializing"
                self._execute_setup(module_id)
                log.info(f"MODULE: 🟢 '{module_id}' is now ACTIVE")
            bus.emit("ui:needs_refresh", {"reason": f"Plugin {module_id} activated."})
        else:
            entry["status"] = "disabled"
            log.info(f"MODULE: 🔴 '{module_id}' is now DISABLED")
            # "Soft" unload: remove UI and call teardown, but keep module in memory.
            self._teardown_ui(module_id)
            entry = self.registry.get(module_id)
            if entry and hasattr(entry["module"], 'teardown'):
                log.info(f"TEARDOWN: Executing teardown function for '{module_id}'")
                teardown_func = entry["module"].teardown
                if inspect.iscoroutinefunction(teardown_func):
                    bus.create_tracked_task(
                        teardown_func(entry["context"]),
                        name=f"module_teardown:{module_id}"
                    )
                else:
                    teardown_func(entry["context"])

            bus.emit("ui:needs_refresh", {"reason": f"Plugin {module_id} deactivated."})
            
        return True

    def get_manifests(self):
        return [entry["manifest"] for entry in self.registry.values()]

    def _teardown_ui(self, module_id: str):
        """Removes a module's UI components without unloading the code."""
        if module_id not in self.registry:
            return
        from main import app as fastapi_app
        manifest = self.registry[module_id]["manifest"]
        if manifest.ui_route:
            log.info(f"TEARDOWN: Removing UI route '{manifest.ui_route}' for '{module_id}'")
            try:
                fastapi_app.router.routes[:] = [
                    route for route in fastapi_app.router.routes
                    if getattr(route, "path", None) != manifest.ui_route
                ]
                fastapi_app.openapi_schema = None
            except Exception as e:
                log.error(f"TEARDOWN_ERROR: Failed to remove UI for '{module_id}': {e}")

    def unload_module(self, module_id: str):
        if module_id not in self.registry:
            return False

        self._teardown_ui(module_id)
        entry = self.registry[module_id]

        # Drop notification endpoints for this plugin (router sync_declared_endpoints
        # will clean up DB rows on next ui:needs_refresh / db:connected pass).
        try:
            from core.components.notification_router.logic.router_service import (
                notification_router,
            )
            notification_router.forget_plugin(module_id)
        except Exception:
            pass

        # --- VENDORING: Clean up the plugin's private dependency path ---
        if entry["manifest"].type == "PLUGIN":
            try:
                plugin_dir_path = Path(entry["module"].__file__).parent
                vendor_path = plugin_dir_path / "vendor"
                vendor_path_str = str(vendor_path)
                if vendor_path.exists() and vendor_path_str in sys.path:
                    log.debug(f"VENDORS: Removing {vendor_path_str} from sys.path for '{module_id}'")
                    sys.path.remove(vendor_path_str)
            except Exception as e:
                log.warning(f"VENDORS: Could not clean up sys.path for '{module_id}': {e}")

        # --- ROBUST UNLOAD: Purge all submodules of the plugin from memory ---
        base_package_name = ".".join(entry["module"].__name__.split('.')[:-1])
        modules_to_delete = [m for m in sys.modules if m.startswith(base_package_name)]
        
        del self.registry[module_id]
        
        for m in modules_to_delete:
            del sys.modules[m]
            
        log.info(f"UNLOAD: Module '{module_id}' unloaded from memory.")
        return True

    async def reload_module(self, module_id: str, module_folder: str | None = None):
        if module_id not in self.registry:
            return False

        entry = self.registry[module_id]
        is_plugin = entry["manifest"].type == "PLUGIN"
        # Default to the currently-loaded folder, but callers may override when an
        # upgrade changed the on-disk folder name: the stale module object still
        # points at the OLD folder, so deriving it here would reload the wrong
        # (now-removed) path. unload still purges by the old package name first.
        if not module_folder:
            module_folder = entry["module"].__name__.split('.')[-2]

        self.unload_module(module_id)
        importlib.invalidate_caches()
        await asyncio.sleep(0.1) # Brief pause to let things settle
        
        success = self.load_module(module_folder, is_plugin=is_plugin)
        
        if success and is_plugin and db_instance.is_connected:
            await self._activate_saved_plugins()
            
        bus.emit("ui:needs_refresh", {"reason": f"Plugin {module_id} reloaded."})
        return success

    def _validate_manifest(self, manifest: ModuleManifest):
        """Validate manifest constraints.

        Returns a tuple ``(issues, critical_issues)`` where ``issues`` is the
        full list of human-readable problems and ``critical_issues`` is the
        subset that must block activation (e.g. incompatible core version,
        missing dependencies, malformed version metadata).
        """
        issues: List[str] = []
        critical: List[str] = []

        if manifest.min_core_version:
            from core.api import __api_version__
            try:
                if Version(manifest.min_core_version) > Version(__api_version__):
                    msg = (
                        f"Requires min core version {manifest.min_core_version}, "
                        f"but running {__api_version__}"
                    )
                    issues.append(msg)
                    critical.append(msg)
            except InvalidVersion:
                msg = f"Invalid min_core_version format: '{manifest.min_core_version}'"
                issues.append(msg)
                critical.append(msg)

        if manifest.dependencies:
            for dep in manifest.dependencies:
                if dep.id not in self.registry:
                    msg = f"Missing dependency: {dep.id}"
                    issues.append(msg)
                    critical.append(msg)

        # Non-critical hygiene checks (warn-only, never block activation).
        if not manifest.description or manifest.description == "No description provided":
            issues.append("Missing 'description' in manifest")
        if not manifest.icon or manifest.icon == "extension":
            issues.append("Missing custom 'icon' in manifest")

        # Soft health-contract check: plugins are encouraged to expose health().
        module_entry = self.registry.get(manifest.id)
        if module_entry and not hasattr(module_entry.get("module"), "health"):
            issues.append("Missing optional 'health(ctx)' function (recommended)")
            log.debug(
                "HEALTH_CONTRACT: Plugin '%s' does not implement health(ctx). "
                "Add 'async def health(ctx) -> PluginHealthStatus' to enable "
                "global health reporting for this plugin.",
                manifest.id,
            )

        return issues, critical

    def _check_dependencies_met(self, manifest: ModuleManifest) -> bool:
        """Check if all plugin dependencies are loaded, active, and version-compatible."""
        if not manifest.dependencies:
            return True
        for dep in manifest.dependencies:
            entry = self.registry.get(dep.id)
            if not entry or entry.get("status") != "active":
                return False

            # Evaluate optional semver constraint declared on the dependency.
            constraint = getattr(dep, "version_constraint", None)
            if constraint and constraint != "*":
                try:
                    spec = SpecifierSet(constraint)
                    dep_version = entry["manifest"].version
                    if Version(dep_version) not in spec:
                        log.error(
                            f"Dependency constraint failed: {manifest.id} requires "
                            f"{dep.id} {constraint}, found {dep_version}"
                        )
                        return False
                except (InvalidSpecifier, InvalidVersion) as e:
                    log.error(
                        f"Invalid version constraint '{constraint}' for {dep.id}: {e}"
                    )
                    return False
                except Exception as e:
                    log.error(
                        f"Invalid version constraint '{constraint}' for {dep.id}: {e}"
                    )
                    return False
        return True

    async def reconcile_desired_plugins(self):
        """Auto-install/update plugins from LYNDRIX_PLUGINS_DESIRED config.
        
        Called after DB connects. Compares desired list against installed
        PluginState records and triggers installs/updates as needed.
        """
        desired = settings.desired_plugin_specs
        if not desired:
            return

        self._ensure_plugin_state_schema()

        from .plugin_service import plugin_service

        log.info(f"RECONCILE: Checking {len(desired)} desired plugin(s)...")

        for spec in desired:
            url = spec["url"]
            version = spec["version"]
            try:
                user, repo = plugin_service._extract_repo_info(url)
            except Exception as e:
                log.warning(f"RECONCILE: Skipping invalid URL '{url}': {e}")
                continue

            safe_name = repo.replace("-", "_")
            plugin_path = plugin_service.plugin_dir / safe_name

            # Check DB state for auto_update preference
            should_update = settings.LYNDRIX_PLUGINS_AUTO_UPDATE
            state = None
            if db_instance.SessionLocal:
                with db_instance.SessionLocal() as session:
                    state = session.query(PluginState).filter(
                        (PluginState.repo_url == url) |
                        (PluginState.module_id.like(f"%{safe_name}%"))
                    ).first()
                    if state and state.auto_update is not None:
                        should_update = state.auto_update
                    if state:
                        state.repo_url = url
                        state.desired_version = version
                        session.commit()

            if not plugin_path.exists():
                log.info(f"RECONCILE: Installing missing desired plugin '{repo}' at '{version}'...")
                await plugin_service.install_plugin(url, version=version)
            elif version != "latest" and (not state or state.installed_version != version):
                log.info(f"RECONCILE: Updating desired plugin '{repo}' to pinned version '{version}'...")
                await plugin_service.install_plugin(url, version=version, upgrade=True)
            elif version == "latest" and should_update:
                log.info(f"RECONCILE: Updating desired plugin '{repo}' to latest...")
                await plugin_service.install_plugin(url, version=version, upgrade=True)
            else:
                log.debug(f"RECONCILE: Plugin '{repo}' already satisfies desired state.")

        log.info("RECONCILE: Desired plugin check complete.")

    def _ensure_plugin_state_schema(self):
        """Apply additive schema upgrades for plugin_states on existing installs."""
        if self._plugin_state_schema_ready or not db_instance.engine:
            return

        required_columns = {
            "installed_version": "VARCHAR(50) NULL",
            "desired_version": "VARCHAR(50) NULL",
            "repo_url": "VARCHAR(500) NULL",
            "auto_update": "BOOLEAN DEFAULT 0",
        }

        with db_instance.engine.begin() as connection:
            PluginState.__table__.create(bind=connection, checkfirst=True)
            PluginVersionHistory.__table__.create(bind=connection, checkfirst=True)
            inspector = sqlalchemy_inspect(connection)
            existing_columns = {
                column["name"] for column in inspector.get_columns(PluginState.__tablename__)
            }

            for column_name, ddl in required_columns.items():
                if column_name not in existing_columns:
                    log.warning(
                        f"PLUGIN_MANAGER: Migrating plugin_states table, adding column '{column_name}'."
                    )
                    connection.execute(
                        text(f"ALTER TABLE {PluginState.__tablename__} ADD COLUMN {column_name} {ddl}")
                    )

        self._plugin_state_schema_ready = True

module_manager = ModuleManager()
