import asyncio
import hvac
import os
from core.bus import bus
from core.logger import get_logger
from config import settings

from .crypto import decrypt_vault_keys, validate_master_key, WeakMasterKeyError, KEY_FILE
from .vault_init import VaultInitializer

log = get_logger("Core:VaultService")

# Delay between connection-failure retries of the initial health check.
_HEALTH_RETRY_SECONDS = 5


class VaultService:
    def __init__(self) -> None:
        # Vault address comes from settings.VAULT_URL (env > .env > Vault > default).
        self.url = settings.VAULT_URL
        self.client = hvac.Client(url=self.url)
        self.is_connected = False
        self.ui_state = "loading"  # loading, needs_init, needs_unseal, ready, error

        # Subscribe to bus events
        bus.subscribe("system:started")(self.check_vault_health)
        bus.subscribe("vault:init_requested")(self.handle_init)
        bus.subscribe("vault:unseal_requested")(self.handle_unseal)

    async def _ensure_lyndrix_mount(self) -> bool:
        """Ensure the isolated 'lyndrix' KV v2 secret engine exists."""
        try:
            mounts = await asyncio.to_thread(self.client.sys.list_mounted_secrets_engines)
            if 'lyndrix/' not in mounts:
                log.info("SETUP: Creating isolated 'lyndrix' Secret-Store (KV v2)...")
                await asyncio.to_thread(
                    self.client.sys.enable_secrets_engine,
                    backend_type='kv',
                    path='lyndrix',
                    options={'version': '2'},
                )
            return True
        except Exception as e:
            log.error(f"ERROR: Mount check failed: {e}", exc_info=True)
            return False

    async def _restore_token_from_keyfile(self) -> None:
        """Reload the root token from the on-disk key file using the auto-unseal key."""
        if not (os.path.exists(KEY_FILE) and os.path.getsize(KEY_FILE) > 0):
            return
        auto_key = settings.LYNDRIX_MASTER_KEY
        if not auto_key:
            return
        try:
            # File read + Argon2 KDF are blocking/CPU-heavy — keep them off the loop.
            def _decrypt() -> dict:
                with open(KEY_FILE, 'rb') as f:
                    return decrypt_vault_keys(auto_key, f.read())

            keys = await asyncio.to_thread(_decrypt)
            self.client.token = keys['root_token']
            log.info("AUTH: Token restored during runtime.")
        except Exception:
            log.error("ERROR: Token restoration failed (Wrong Master Key?)")

    async def check_vault_health(self, payload: dict | None = None) -> None:
        log.info("CHECK: Checking Vault status...")
        try:
            if not await asyncio.to_thread(self.client.sys.is_initialized):
                self.ui_state = "needs_init"
                log.warning("WARNING: Vault is not initialized yet!")
                bus.emit("vault:needs_init", {})
                return

            if await asyncio.to_thread(self.client.sys.is_sealed):
                self.ui_state = "needs_unseal"
                log.info("LOCKED: Vault is sealed. Waiting for key...")
                bus.emit("vault:needs_unseal", {})
            else:
                self.ui_state = "ready"
                self.is_connected = True

                await self._restore_token_from_keyfile()

                mount_success = await self._ensure_lyndrix_mount()
                if not mount_success:
                    log.error("CRITICAL: Vault token is invalid or lacks permissions. Halting boot sequence for Vault.")
                    bus.emit("vault:auth_failed", {})
                    return

                log.info("SUCCESS: Vault is already open and ready.")
                bus.emit("vault:opened", {})
                bus.emit("vault:ready_for_data", {})  # Plugins may load now
        except Exception as e:
            # Connection failure (Vault down/unreachable) must not silently hang the
            # boot chain: surface an explicit error state and schedule a retry.
            log.error(f"ERROR: Vault connection error: {e}", exc_info=True)
            self.ui_state = "error"
            bus.emit("vault:connection_failed", {"reason": "unreachable"})
            bus.emit(
                "system:maintenance_mode",
                {"service": "vault", "active": True, "reason": "unreachable"},
            )
            bus.create_tracked_task(
                self._retry_health_check(),
                name="vault_service:health_retry",
            )

    async def _retry_health_check(self) -> None:
        """Retry the health check after a delay when Vault was unreachable."""
        await asyncio.sleep(_HEALTH_RETRY_SECONDS)
        if not self.is_connected:
            log.info("RETRY: Re-checking Vault status after connection failure...")
            await self.check_vault_health()

    async def handle_init(self, payload: dict) -> None:
        key = payload.get("key")
        log.info("INIT: Initializing new Vault...")
        try:
            validate_master_key(key)
            init_helper = VaultInitializer(self.url)
            keys = await asyncio.to_thread(init_helper.setup_fresh_vault, key)
            self.client.token = keys['root_token']
            await asyncio.to_thread(self.client.sys.submit_unseal_keys, keys['unseal_keys'])

            # IMPORTANT: create mount after init
            await self._ensure_lyndrix_mount()

            self.ui_state = "ready"
            self.is_connected = True
            bus.emit("vault:opened", {})
            bus.emit("vault:ready_for_data", {})
        except WeakMasterKeyError as e:
            log.warning(f"INIT: Rejected weak master key: {e}")
            bus.emit("vault:init_failed", {"reason": "weak_key"})
        except Exception as e:
            log.error(f"CRITICAL: Init failed: {e}", exc_info=True)
            bus.emit("vault:init_failed", {"reason": "error"})

    async def handle_unseal(self, payload: dict) -> None:
        key = payload.get("key")
        try:
            # File read + Argon2 KDF are blocking/CPU-heavy — keep them off the loop.
            def _decrypt() -> dict:
                with open(KEY_FILE, 'rb') as f:
                    return decrypt_vault_keys(key, f.read())

            keys = await asyncio.to_thread(_decrypt)

            self.client.token = keys['root_token']
            await asyncio.to_thread(self.client.sys.submit_unseal_keys, keys['unseal_keys'])

            success = await self._ensure_lyndrix_mount()
            if success:
                self.ui_state = "ready"
                self.is_connected = True
                log.info("UNSEAL: Vault successfully unsealed and ready!")
                bus.emit("vault:opened", {})
                bus.emit("vault:ready_for_data", {})  # Plugins may load now
            else:
                bus.emit("vault:unseal_failed", {"reason": "mount_failed"})
        except Exception as e:
            # Wrong key (GCM verify) or Vault error: signal the UI so it does not
            # wait forever on a status that never changes.
            log.error(f"ERROR: Unseal failed: {e}", exc_info=True)
            bus.emit("vault:unseal_failed", {"reason": "incorrect_key"})


vault_instance = VaultService()
