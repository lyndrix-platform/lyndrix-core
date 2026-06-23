"""Shared Vault KV-v2 read/write helpers.

Centralizes access to the ``lyndrix`` KV mount so the same error handling and
logging apply everywhere (plugin settings API, ModuleContext, …) instead of each
call site wrapping ``vault_instance.client.secrets.kv.v2`` in its own try/except.

A missing secret (never written) is normal and returns ``{}`` quietly; only
unexpected failures are logged as warnings/errors.
"""
from __future__ import annotations

from typing import Any, Dict

import hvac.exceptions

from core.logger import get_logger

log = get_logger("Core:VaultKV")

_MOUNT = "lyndrix"


def read_secret_dict(path: str) -> Dict[str, Any]:
    """Return all key/values stored at ``lyndrix/{path}``.

    Returns ``{}`` when Vault is unreachable or the secret does not exist yet.
    Unexpected errors are logged (the caller still gets ``{}``) so a Vault
    outage is visible in the logs rather than silently masked.
    """
    from core.services import vault_instance

    if not vault_instance.is_connected:
        return {}
    try:
        resp = vault_instance.client.secrets.kv.v2.read_secret_version(
            path=path, mount_point=_MOUNT
        )
        data = ((resp or {}).get("data", {}) or {}).get("data", {}) or {}
        return dict(data)
    except hvac.exceptions.InvalidPath:
        # Secret has never been written — a normal "no settings yet" state.
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"Vault read failed for '{path}': {exc}")
        return {}


def write_secret_dict(path: str, data: Dict[str, Any]) -> bool:
    """Create-or-update the secret at ``lyndrix/{path}`` with ``data``.

    Returns ``True`` on success, ``False`` when Vault is unreachable or the
    write fails (error logged). Callers that must surface failure to the user
    (e.g. an API 5xx) should check the return value.
    """
    from core.services import vault_instance

    if not vault_instance.is_connected:
        log.error(f"Vault not connected — cannot write '{path}'.")
        return False
    try:
        vault_instance.client.secrets.kv.v2.create_or_update_secret(
            path=path, mount_point=_MOUNT, secret=data
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.error(f"Vault write failed for '{path}': {exc}")
        return False
