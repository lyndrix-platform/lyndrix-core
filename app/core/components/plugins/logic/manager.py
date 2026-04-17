import os
import sys
import importlib
from pathlib import Path
import inspect
import hashlib
import shutil
import subprocess
import tempfile
import asyncio
from core.logger import get_logger
from core.bus import bus
from core.components.database.logic.db_service import db_instance
from .models import ModuleManifest, PluginState
from .context import ModuleContext

log = get_logger("Core:ModuleManager")

class ModuleManager:
    def __init__(self):
        self.registry = {}
        current_file_path = os.path.abspath(__file__)
        self.base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_file_path)))))
        # Subscribe to filesystem change events from the PluginService
        bus.subscribe("plugin:files_changed")(self._handle_plugin_change)


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
        # Since BootService waits for the DB to connect before calling load_all, 
        # we know the database is ready here.
        if db_instance.is_connected:
            asyncio.create_task(self._activate_saved_plugins())
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
                
                # --- SELF-BOOTSTRAP DEPENDENCIES ---
                # If a plugin has requirements.txt but no vendor folder, install them now.
                # This blocks startup but ensures dependencies are ready before import.
                if is_plugin:
                    req_file = Path(item_path) / "requirements.txt"
                    vendor_dir = Path(item_path) / "vendor"
                    receipt_file = vendor_dir / ".receipt"

                    if req_file.exists():
                        with open(req_file, 'rb') as f:
                            current_checksum = hashlib.sha256(f.read()).hexdigest()
                        
                        last_install_checksum = ""
                        if receipt_file.exists():
                            with open(receipt_file, 'r') as f:
                                last_install_checksum = f.read().strip()

                        if current_checksum != last_install_checksum:
                            log.info(f"VENDORS: Requirements changed for '{item}'. Re-installing dependencies...")
                            if vendor_dir.exists():
                                shutil.rmtree(vendor_dir)
                            
                            vendor_dir.mkdir()
                            try:
                                subprocess.run(
                                    [sys.executable, "-m", "pip", "install", "--target", str(vendor_dir), "-r", str(req_file)],
                                    capture_output=True, text=True, check=True, encoding='utf-8'
                                )
                                with open(receipt_file, 'w') as f:
                                    f.write(current_checksum)
                                log.info(f"VENDORS: Bootstrap complete for '{item}'.")
                            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                                stderr = getattr(e, 'stderr', str(e))
                                log.error(f"PIP_ERROR: Failed to bootstrap dependencies for '{item}': {stderr}")
                                if vendor_dir.exists():
                                    shutil.rmtree(vendor_dir) # Clean up failed install

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
                    sys.path.insert(0, vendor_path_str)
                    path_added = True
        
        try:
            module = importlib.import_module(full_module_path)
            
            if not hasattr(module, 'manifest'):
                log.warning(f"VALIDATION_ERROR: Module '{module_name}' missing 'manifest'")
                return False

            raw_manifest = module.manifest
            manifest = ModuleManifest(**raw_manifest) if isinstance(raw_manifest, dict) else raw_manifest

            if manifest.id in self.registry:
                return False

            ctx = ModuleContext(manifest)

            # Register as initializing/parked
            self.registry[manifest.id] = {
                "manifest": manifest,
                "module": module,
                "context": ctx,
                "status": "initializing" 
            }

            if not is_plugin:
                # Core modules boot instantly without waiting for the DB
                self._execute_setup(manifest.id)
                log.info(f"LOAD_SUCCESS: CORE '{manifest.name}' is online")
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
            self.load_module(module_name, is_plugin=True)
            await self._activate_saved_plugins() # Re-check DB state for the new plugin
        elif action == "uninstall":
            module_id = payload.get("id")
            self.unload_module(module_id)

    def _execute_setup(self, module_id):
        """Helper to safely execute the module's setup function."""
        entry = self.registry.get(module_id)
        if not entry:
            return

        module = entry["module"]
        ctx = entry["context"]
        
        if hasattr(module, 'setup'):
            if inspect.iscoroutinefunction(module.setup):
                asyncio.create_task(self._safe_async_setup(module, ctx, module_id))
            else:
                module.setup(ctx)
                entry["status"] = "active"

    async def _safe_async_setup(self, module, ctx, module_id):
        try:
            await module.setup(ctx)
            if module_id in self.registry:
                self.registry[module_id]["status"] = "active"
        except Exception as e:
            log.error(f"RUNTIME_ERROR: Async setup failed for '{module_id}': {e}")

    async def _activate_saved_plugins(self, payload=None):
        """Called when DB connects. Reads states and boots active plugins."""
        log.info("PLUGIN_MANAGER: Verifying plugin states from Database...")
        
        if not db_instance.SessionLocal:
            log.error("PLUGIN_MANAGER: SessionLocal missing during DB activation.")
            return

        with db_instance.SessionLocal() as session:
            for module_id, entry in self.registry.items():
                if entry["manifest"].type == "CORE":
                    continue
                
                # Fetch state from DB
                db_state = session.query(PluginState).filter(PluginState.module_id == module_id).first()
                
                # If it doesn't exist in DB, create it as disabled
                if not db_state:
                    db_state = PluginState(module_id=module_id, is_active=False)
                    session.add(db_state)
                    session.commit()

                if db_state.is_active:
                    log.info(f"DB_RESTORE: Activating plugin '{module_id}'")
                    self._execute_setup(module_id)
                else:
                    log.info(f"DB_RESTORE: Plugin '{module_id}' remains disabled.")
                    entry["status"] = "disabled"

    def toggle_module(self, module_id: str, active: bool):
        """Toggles the module state in RAM and persists it to the DB."""
        if module_id not in self.registry:
            return False
            
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

        # 2. Update RAM status
        entry = self.registry[module_id]
        entry["status"] = "active" if active else "disabled"
        status_icon = "🟢" if active else "🔴"
        log.info(f"MODULE: {status_icon} '{module_id}' is now {'ACTIVE' if active else 'DISABLED'}")
        
        # 3. Boot it up if newly activated
        if active:
            self._execute_setup(module_id)
            bus.emit("ui:needs_refresh", {"reason": f"Plugin {module_id} activated."})
        else:
            # "Soft" unload: remove UI and call teardown, but keep module in memory.
            self._teardown_ui(module_id)
            entry = self.registry.get(module_id)
            if entry and hasattr(entry["module"], 'teardown'):
                log.info(f"TEARDOWN: Executing teardown function for '{module_id}'")
                teardown_func = entry["module"].teardown
                if inspect.iscoroutinefunction(teardown_func):
                    asyncio.create_task(teardown_func(entry["context"]))
                else:
                    teardown_func(entry["context"])

            bus.emit("ui:needs_refresh", {"reason": f"Plugin {module_id} deactivated."})
            
        return True

    def get_manifests(self):
        return [entry["manifest"] for entry in self.registry.values()]

    def _teardown_ui(self, module_id: str):
        """Removes a module's UI components without unloading the code."""
        if module_id not in self.registry: return
        from main import app as fastapi_app
        from nicegui import ui
        manifest = self.registry[module_id]["manifest"]
        if manifest.ui_route:
            log.info(f"TEARDOWN: Removing UI route '{manifest.ui_route}' for '{module_id}'")
            try:
                fastapi_app.routes = [route for route in fastapi_app.routes if route.path != manifest.ui_route]
            except Exception as e:
                log.error(f"TEARDOWN_ERROR: Failed to remove UI for '{module_id}': {e}")

    def unload_module(self, module_id: str):
        if module_id not in self.registry:
            return False

        self._teardown_ui(module_id)
        entry = self.registry[module_id]

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

    async def reload_module(self, module_id: str):
        if module_id not in self.registry:
            return False
            
        entry = self.registry[module_id]
        is_plugin = entry["manifest"].type == "PLUGIN"
        module_folder = entry["module"].__name__.split('.')[-2]

        self.unload_module(module_id)
        await asyncio.sleep(0.1) # Brief pause to let things settle
        
        success = self.load_module(module_folder, is_plugin=is_plugin)
        
        if success and is_plugin and db_instance.is_connected:
            await self._activate_saved_plugins()
            
        bus.emit("ui:needs_refresh", {"reason": f"Plugin {module_id} reloaded."})
        return success

module_manager = ModuleManager()