import os
import json
from argon2 import low_level
from Crypto.Cipher import AES
from config import settings

# Configuration via environment (12-Factor App)
KEY_FILE = settings.LYNDRIX_VAULT_KEY_FILE

# Argon2 parameters (tunable for weak hardware vs. high-security)
ARGON_TIME = settings.LYNDRIX_ARGON_TIME
ARGON_MEM = settings.LYNDRIX_ARGON_MEM
ARGON_PARALLEL = settings.LYNDRIX_ARGON_PARALLEL

# The master key is the sole protection for the Argon2/AES-GCM blob that stores
# the Vault root token, so enforce a meaningful minimum strength everywhere a key
# is set (UI, API, auto-init).
MIN_MASTER_KEY_LENGTH = 12


class WeakMasterKeyError(ValueError):
    """Raised when a master key does not meet the minimum strength policy."""


def validate_master_key(master_key: str | None) -> None:
    """Enforce the master-key strength policy. Raises WeakMasterKeyError on failure."""
    if not master_key:
        raise WeakMasterKeyError("Master key must not be empty.")
    if len(master_key) < MIN_MASTER_KEY_LENGTH:
        raise WeakMasterKeyError(
            f"Master key must be at least {MIN_MASTER_KEY_LENGTH} characters long."
        )
    # Reject trivially low-entropy keys (e.g. a single repeated character).
    if len(set(master_key)) < 4:
        raise WeakMasterKeyError("Master key has insufficient character variety.")


def derive_key(master_key: str, salt: bytes) -> bytes:
    return low_level.hash_secret_raw(
        secret=master_key.encode(),
        salt=salt,
        time_cost=ARGON_TIME, memory_cost=ARGON_MEM, parallelism=ARGON_PARALLEL,
        hash_len=32, type=low_level.Type.ID
    )

def encrypt_vault_keys(master_key: str, vault_payload: dict) -> bytes:
    # Enforce the strength policy at the point of encryption so no entry path can
    # persist a blob protected by a weak key.
    validate_master_key(master_key)
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
