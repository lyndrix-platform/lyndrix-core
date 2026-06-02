import os
import json
from argon2 import low_level
from Crypto.Cipher import AES
from config import settings

# Konfiguration via Environment (12-Factor App)
KEY_FILE = settings.LYNDRIX_VAULT_KEY_FILE

# Argon2 Parameter (anpassbar für schwache Hardware vs. High-Security)
ARGON_TIME = settings.LYNDRIX_ARGON_TIME
ARGON_MEM = settings.LYNDRIX_ARGON_MEM
ARGON_PARALLEL = settings.LYNDRIX_ARGON_PARALLEL

def derive_key(master_key: str, salt: bytes) -> bytes:
    return low_level.hash_secret_raw(
        secret=master_key.encode(),
        salt=salt,
        time_cost=ARGON_TIME, memory_cost=ARGON_MEM, parallelism=ARGON_PARALLEL, 
        hash_len=32, type=low_level.Type.ID
    )

def encrypt_vault_keys(master_key: str, vault_payload: dict) -> bytes:
    salt = os.urandom(16)
    key = derive_key(master_key, salt)
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(json.dumps(vault_payload).encode())
    return salt + cipher.nonce + tag + ciphertext

def decrypt_vault_keys(master_key: str, encrypted_blob: bytes) -> dict:
    salt, nonce, tag, ciphertext = encrypted_blob[:16], encrypted_blob[16:32], encrypted_blob[32:48], encrypted_blob[48:]
    key = derive_key(master_key, salt)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return json.loads(cipher.decrypt_and_verify(ciphertext, tag).decode())