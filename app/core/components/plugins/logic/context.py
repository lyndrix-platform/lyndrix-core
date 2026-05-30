from collections import defaultdict
import threading

import hvac
from fastapi import APIRouter
from core.bus import bus as global_bus
from core.logger import get_logger
from core.services import vault_instance
from .models import ModuleManifest

class ModuleContext:
    """
    Der isolierte Sandkasten für JEDES Modul (Core & Plugin).
    """
    _vault_write_locks = defaultdict(threading.RLock)

    def __init__(self, manifest: ModuleManifest):
        self.manifest = manifest
        # Logger zeigt z.B. [Core: IAM] oder [Plugin: Discord]
        prefix = "Core" if manifest.type == "CORE" else "Plugin"
        self.log = get_logger(f"{prefix}:{manifest.name}")
        self.state = {}

    # --- EVENT BUS PROXY ---

    def subscribe(self, topic: str):
        def decorator(callback):
            if topic in self.manifest.permissions.subscribe or "*" in self.manifest.permissions.subscribe:
                self.log.debug(f"SUBSCRIBE: Subscribed to topic: {topic}")
                global_bus.subscribe(topic)(callback)
            else:
                message = (
                    f"Plugin '{self.manifest.id}' is not permitted to subscribe to "
                    f"'{topic}'. Declare it in permissions.subscribe."
                )
                self.log.error(message)
                raise PermissionError(message)
            return callback
        return decorator

    def emit(self, topic: str, payload: dict = None):
        if topic in self.manifest.permissions.emit or "*" in self.manifest.permissions.emit:
            global_bus.emit(topic, payload)
        else:
            message = (
                f"Plugin '{self.manifest.id}' is not permitted to emit topic "
                f"'{topic}'. Declare it in permissions.emit."
            )
            self.log.error(message)
            raise PermissionError(message)

    def create_task(self, coro, *, name: str = None):
        """Create an observed background task owned by this module."""
        task_name = name or f"module:{self.manifest.id}"
        return global_bus.create_tracked_task(coro, name=task_name)

    # --- HTTP ROUTE REGISTRATION ---

    def register_routes(self, router: APIRouter) -> None:
        """
        Mount a FastAPI ``APIRouter`` for this plugin.

        Routes are prefixed at ``/api/plugins/<module-id>/`` and appear in
        the OpenAPI schema automatically.  Call this inside ``setup(ctx)``.

        Example::

            from core.api import APIRouter

            router = APIRouter()

            @router.get("/status")
            def status():
                return {"ok": True}

            def setup(ctx):
                ctx.register_routes(router)
        """
        # Import here to avoid a circular import at module load time.
        from core.api.router_registry import router_registry

        router_registry.register(self.manifest.id, router)
        self.log.info("ROUTES: Registered HTTP router at /api/plugins/%s/", self.manifest.id)

    # --- VAULT PROXY (Hier war der Einrückungsfehler) ---

    def _get_vault_path(self) -> str:
        """Bestimmt den Basis-Pfad für dieses Modul im Vault."""
        folder = "core" if self.manifest.type == "CORE" else "plugins"
        return f"{folder}/{self.manifest.id}"

    def get_secret(self, key: str) -> str:
        """Lädt einen einzelnen Wert aus dem KV-V2 Store."""
        if not vault_instance.is_connected:
            return None
            
        path = self._get_vault_path()
        try:
            # hvac v2 read: mount_point 'lyndrix' ist das Haupt-Regal
            response = vault_instance.client.secrets.kv.v2.read_secret_version(
                path=path,
                mount_point="lyndrix"
            )
            # In V2 liegen die echten Daten gekapselt in ['data']['data']
            if response and 'data' in response and 'data' in response['data']:
                return response['data']['data'].get(key)
        except Exception:
            return None
        return None

    def set_secret(self, key: str, value: str):
        """Speichert einen Wert im KV-V2 Store, ohne andere Keys zu löschen."""
        if not vault_instance.is_connected:
            self.log.error("VAULT: Vault not connected.")
            return False
            
        path = self._get_vault_path()
        try:
            lock = self._vault_write_locks[path]
            with lock:
                # Serialize read-modify-write updates per Vault path so concurrent
                # plugin writes do not overwrite each other.
                current_data = {}
                try:
                    response = vault_instance.client.secrets.kv.v2.read_secret_version(
                        path=path, mount_point="lyndrix"
                    )
                    current_data = response['data']['data']
                except Exception:
                    pass

                current_data[key] = value

                vault_instance.client.secrets.kv.v2.create_or_update_secret(
                    path=path,
                    mount_point="lyndrix",
                    secret=current_data
                )
            self.log.info(f"SUCCESS: Secret '{key}' persisted securely at '{path}'.")
            return True
        except Exception as e:
            self.log.error(f"ERROR: Failed to write to Vault: {e}", exc_info=True)
            return False