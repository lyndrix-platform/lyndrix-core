import hvac
import os

# FIX: Relativer Import
from .crypto import encrypt_vault_keys, KEY_FILE

class VaultInitializer:
    def __init__(self, url: str):
        self.url = url

    def get_client(self):
        return hvac.Client(url=self.url)

    def check_status(self):
        """Prüft, ob der Vault initialisiert und versiegelt ist."""
        client = self.get_client()
        try:
            return {
                "initialized": client.sys.is_initialized(),
                "sealed": client.sys.is_sealed()
            }
        except Exception:
            return {"initialized": False, "sealed": True, "error": "Connection failed"}

    def setup_fresh_vault(self, master_key: str):
        """Initialisiert einen komplett neuen Vault und speichert die .enc Datei."""
        client = self.get_client()
        if client.sys.is_initialized():
            raise Exception("Vault is already initialized!")

        # Initialisierung (1 Share für Homelab)
        res = client.sys.initialize(secret_shares=1, secret_threshold=1)
        keys = {
            "root_token": res['root_token'],
            "unseal_keys": res['keys']
        }

        # Verschlüsseln und Speichern
        blob = encrypt_vault_keys(master_key, keys)
        os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
        with open(KEY_FILE, 'wb') as f:
            f.write(blob)
        
        return keys