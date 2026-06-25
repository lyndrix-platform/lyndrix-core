from collections import defaultdict
from datetime import datetime, timezone
import threading
from uuid import UUID

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

    def notify(
        self,
        endpoint_name: str,
        payload: dict | None = None,
        *,
        notification_id: str | None = None,
        correlation_id: UUID | None = None,
        title: str | None = None,
        body: str | None = None,
        severity: str = "info",
        clear: bool = False,
    ) -> None:
        """Emit a notification through the central router.

        The endpoint must be declared in this plugin's manifest
        ``notification_endpoints`` list. The router consumes the resulting
        envelope on the ``notification:routed`` topic and applies the user's
        configured routing (internal toast/history and/or external provider).
        """
        declared_names = {ep.name for ep in self.manifest.notification_endpoints}
        if endpoint_name not in declared_names:
            message = (
                f"Plugin '{self.manifest.id}' is not permitted to notify endpoint "
                f"'{endpoint_name}'. Declare it in manifest.notification_endpoints."
            )
            self.log.error(message)
            raise PermissionError(message)

        envelope = {
            "envelope_version": "1",
            "source_plugin_id": self.manifest.id,
            "endpoint_name": endpoint_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
            "notification_id": notification_id,
            "correlation_id": str(correlation_id) if correlation_id else None,
            "title": title,
            "body": body,
            "severity": severity,
            "clear": clear,
        }
        global_bus.emit("notification:routed", envelope)

    def register_gateway_adapter(self, adapter) -> None:
        """Register a GatewayAdapter into the central MessagingGateway.

        Call this in your plugin's ``setup()`` hook after constructing the
        adapter::

            from core.api import GatewayAdapter, GatewayCapability, OutboundMessage

            class MyAdapter(GatewayAdapter):
                provider_id  = "myservice"
                display_name = "My Service"
                capabilities = GatewayCapability.TEXT

                async def send(self, message: OutboundMessage) -> str | None:
                    ...

            def setup(ctx):
                ctx.register_gateway_adapter(MyAdapter(ctx))
        """
        from core.components.messaging.gateway import messaging_gateway
        messaging_gateway.register(adapter)
        self.log.info("GATEWAY: Registered adapter '%s'.", adapter.provider_id)

    # --- HTTP ROUTE REGISTRATION ---

    def register_theme_overrides(self, overrides: dict) -> None:
        """Register partial UIStyles overrides scoped to this plugin.

        Keys must match UIStyles class attribute names (e.g. ``"CARD_BASE"``).
        Overrides are applied only when the plugin's own pages call
        ``resolve_component_styles(plugin_id=self.manifest.id)``.
        They do not affect the global UIStyles class or other plugins.

        Example::

            def setup(ctx):
                ctx.register_theme_overrides({
                    "CARD_BASE": "p-4 rounded-xl border border-zinc-700 bg-zinc-900",
                })
        """
        from core.theming import get_theme_engine
        get_theme_engine().register_plugin_overrides(self.manifest.id, overrides)
        self.log.debug("THEME: Registered %d style override(s).", len(overrides))

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

    def _get_settings_vault_path(self) -> str:
        """Pfad für schema-driven settings (getrennt von rohen Secrets)."""
        folder = "core" if self.manifest.type == "CORE" else "plugins"
        return f"{folder}/{self.manifest.id}/settings"

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

    def get_setting(self, key: str, default=None):
        """Read one value from this plugin's schema-driven settings in Vault.

        Settings are written by the React UI via PUT /api/plugins/{id}/settings
        and stored at a separate Vault path from raw secrets (set_secret).
        Returns *default* when Vault is unreachable or the key has no value.

        Example::

            def setup(ctx):
                @ctx.subscribe("vault:ready_for_data")
                async def _load(_payload=None):
                    ctx.state["timeout"] = ctx.get_setting("timeout_s", default=30)
        """
        from core.components.vault.logic.kv_helper import read_secret_dict

        data = read_secret_dict(self._get_settings_vault_path())
        return data.get(key, default)

    def get_all_settings(self) -> dict:
        """Read all schema-driven settings for this plugin from Vault.

        Returns an empty dict when Vault is unreachable or no settings have
        been saved yet.  Useful for bulk-loading on ``vault:ready_for_data``.

        Example::

            def setup(ctx):
                @ctx.subscribe("vault:ready_for_data")
                async def _load(_payload=None):
                    cfg = ctx.get_all_settings()
                    ctx.state["api_url"] = cfg.get("api_url", "http://localhost")
        """
        from core.components.vault.logic.kv_helper import read_secret_dict

        return read_secret_dict(self._get_settings_vault_path())

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