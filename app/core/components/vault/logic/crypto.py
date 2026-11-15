import os
import json
import logging
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


# --- Key file storage (SC-01) -------------------------------------------------
# The blob holds the Vault root token and unseal key. Its only protection besides
# the master key is the file mode, so it is owner-only from the first byte on.
KEY_FILE_MODE = 0o600
KEY_DIR_MODE = 0o700

_log = logging.getLogger("Core:VaultCrypto")


def write_key_file(blob: bytes, path: str | None = None) -> None:
    """Persist the encrypted key blob atomically with mode 0600.

    The temp file is created with the final mode (never widened by the umask) and
    renamed into place, so no reader ever sees a partial or world-readable file.
    """
    path = path or KEY_FILE
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=KEY_DIR_MODE, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, KEY_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        # O_CREAT honours the umask and keeps the mode of a leftover temp file.
        os.chmod(tmp_path, KEY_FILE_MODE)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def harden_key_storage(path: str | None = None) -> None:
    """Tighten an existing key file (0600) and its directory (0700) on boot.

    Earlier releases wrote the file with the default umask and the entrypoint made
    the directory world-writable. Best effort: a read-only or foreign-owned mount
    (e.g. a Kubernetes secret volume) must not stop the boot.
    """
    path = path or KEY_FILE
    for target, mode, wanted in (
        (os.path.dirname(path), KEY_DIR_MODE, os.path.isdir),
        (path, KEY_FILE_MODE, os.path.isfile),
    ):
        try:
            if target and wanted(target) and (os.stat(target).st_mode & 0o777) != mode:
                os.chmod(target, mode)
        except OSError as exc:
            _log.warning(f"Could not restrict permissions of {target}: {exc}")
