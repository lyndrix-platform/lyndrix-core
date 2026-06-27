import hvac
import os

from .crypto import encrypt_vault_keys, KEY_FILE


class VaultAlreadyInitializedError(RuntimeError):
    """Raised when attempting to initialize a Vault that is already initialized."""


class VaultInitializer:
    def __init__(self, url: str):
        self.url = url

    def get_client(self) -> hvac.Client:
        return hvac.Client(url=self.url)

    def check_status(self) -> dict:
        """Check whether the Vault is initialized and sealed."""
        client = self.get_client()
        try:
            return {
                "initialized": client.sys.is_initialized(),
                "sealed": client.sys.is_sealed()
            }
        except Exception:
            return {"initialized": False, "sealed": True, "error": "Connection failed"}

    def setup_fresh_vault(self, master_key: str) -> dict:
        """Initialize a brand-new Vault and persist the encrypted .enc key file."""
        client = self.get_client()
        if client.sys.is_initialized():
            raise VaultAlreadyInitializedError("Vault is already initialized!")

        # Initialization (1 share for homelab use)
        res = client.sys.initialize(secret_shares=1, secret_threshold=1)
        keys = {
            "root_token": res['root_token'],
            "unseal_keys": res['keys']
        }

        # Encrypt and store (encrypt_vault_keys enforces the master-key policy)
        blob = encrypt_vault_keys(master_key, keys)
        os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
        with open(KEY_FILE, 'wb') as f:
            f.write(blob)

        return keys
