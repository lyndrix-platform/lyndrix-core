import os
from core.bus import bus
from core.logger import get_logger
from config import settings

# IMPORTANT: Relative import for the file in the same directory
from .crypto import KEY_FILE

log = get_logger("Core:AutoUnseal")

class AutoUnsealManager:
    def __init__(self):
        bus.subscribe("vault:needs_init")(self.on_needs_init)
        bus.subscribe("vault:needs_unseal")(self.on_needs_unseal)

    async def on_needs_init(self, payload):
        auto_key = settings.LYNDRIX_MASTER_KEY
        if auto_key:
            log.info("INIT: Fresh Vault detected & Master Key provided. Starting AUTO-INIT...")
            bus.emit("vault:init_requested", {"key": auto_key})
        else:
            log.info("INFO: No LYNDRIX_MASTER_KEY in ENV. Waiting for manual input.")

    async def on_needs_unseal(self, payload):
        auto_key = settings.LYNDRIX_MASTER_KEY
        if not auto_key:
            log.info("INFO: No LYNDRIX_MASTER_KEY in ENV. Waiting for manual input.")
            return
            
        if os.path.exists(KEY_FILE):
            log.info("UNSEAL: Keyfile found. Starting AUTO-UNSEAL...")
            bus.emit("vault:unseal_requested", {"key": auto_key})
        else:
            log.warning("UNSEAL: Master key found, but no KEY_FILE exists. Manual unseal required.")

# Instance creation
auto_unseal_manager = AutoUnsealManager()