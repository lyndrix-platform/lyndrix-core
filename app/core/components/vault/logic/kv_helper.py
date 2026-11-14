"""Shared Vault KV-v2 read/write helpers.

Centralizes access to the ``lyndrix`` KV mount so the same error handling and
logging apply everywhere (plugin settings API, ModuleContext, …) instead of each
call site wrapping ``vault_instance.client.secrets.kv.v2`` in its own try/except.

A missing secret (never written) is normal and returns ``{}`` quietly; only
unexpected failures are logged as warnings/errors.

Two kinds of callers, two contracts:

* **Pure readers** use :func:`read_secret_dict` — tolerant, ``{}`` on any failure.
* **Read-modify-write** callers use :func:`update_secret_dict` (or
  :func:`read_secret_strict` + :func:`write_secret_dict` with ``cas=``). A KV v2
  write replaces the WHOLE secret, so a writer must never treat "the read failed"
  as "the secret is empty": that turned one transient Vault 5xx into the loss of
  every other key at the path. The strict read raises :class:`VaultReadError`
  instead, and the write is guarded by check-and-set so two instances updating
  the same path cannot silently overwrite each other.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Dict, Optional, Tuple

import hvac.exceptions

from core.logger import get_logger

log = get_logger("Core:VaultKV")

_MOUNT = "lyndrix"

# A CAS conflict means "somebody else wrote in between" — re-read and re-apply.
_CAS_ATTEMPTS = 5


class VaultReadError(RuntimeError):
    """The secret could not be read (Vault down/sealed/5xx) — its content is UNKNOWN.

    Subclasses ``RuntimeError`` because the settings callers already map that to
    HTTP 503 / a UI warning.
    """


class VaultWriteError(RuntimeError):
    """The secret could not be written (Vault error or CAS retries exhausted)."""


class VaultCasConflict(VaultWriteError):
    """Check-and-set mismatch: the secret changed since it was read."""


def _is_cas_conflict(exc: Exception) -> bool:
    # Vault answers 400 "check-and-set parameter did not match the current version".
    return isinstance(exc, hvac.exceptions.InvalidRequest) and "check-and-set" in str(exc).lower()


def _current_version(client: Any, path: str) -> int:
    """Version to use as ``cas`` for a path whose latest version has no data.

    ``0`` = the path was never written (``cas=0`` means "create only"). A path
    whose latest version was soft-deleted still has a version counter, and the
    CAS must name it.
    """
    try:
        meta = client.secrets.kv.v2.read_secret_metadata(path=path, mount_point=_MOUNT)
        return int(((meta or {}).get("data") or {}).get("current_version") or 0)
    except hvac.exceptions.InvalidPath:
        return 0


def read_secret_strict(path: str) -> Tuple[Dict[str, Any], int]:
    """Return ``(data, version)`` of ``lyndrix/{path}`` — or raise.

    ``({}, 0)`` means the path is really absent. Any other failure (Vault not
    connected, sealed, timeout, 5xx, permission) raises :class:`VaultReadError`,
    so a read-modify-write caller can abort instead of writing over data it never saw.
    """
    from core.services import vault_instance

    if not vault_instance.is_connected:
        raise VaultReadError(f"Vault not connected — cannot read '{path}'")
    client = vault_instance.client
    try:
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=_MOUNT, raise_on_deleted_version=True
            )
        except hvac.exceptions.InvalidPath:
            return {}, _current_version(client, path)
        outer = (resp or {}).get("data") or {}
        version = int((outer.get("metadata") or {}).get("version") or 0)
        return dict(outer.get("data") or {}), version
    except VaultReadError:
        raise
    except Exception as exc:
        raise VaultReadError(f"Vault read failed for '{path}': {exc}") from exc


def read_secret_dict(path: str) -> Dict[str, Any]:
    """Return all key/values stored at ``lyndrix/{path}`` — tolerant reader.

    Returns ``{}`` when Vault is unreachable or the secret does not exist yet.
    Unexpected errors are logged (the caller still gets ``{}``) so a Vault
    outage is visible in the logs rather than silently masked.

    Only for callers that READ. Never feed the result into a write: use
    :func:`update_secret_dict`, which can tell "absent" from "unreadable".
    """
    from core.services import vault_instance

    if not vault_instance.is_connected:
        return {}
    try:
        return read_secret_strict(path)[0]
    except VaultReadError as exc:
        log.warning(str(exc))
        return {}


def write_secret_dict(path: str, data: Dict[str, Any], *, cas: Optional[int] = None) -> bool:
    """Create-or-update the secret at ``lyndrix/{path}`` with ``data``.

    Returns ``True`` on success, ``False`` when Vault is unreachable or the
    write fails (error logged). Callers that must surface failure to the user
    (e.g. an API 5xx) should check the return value.

    With ``cas`` (the version from :func:`read_secret_strict`) the write only
    succeeds while the secret is still at that version; a mismatch raises
    :class:`VaultCasConflict` so the caller can re-read and retry.
    """
    from core.services import vault_instance

    if not vault_instance.is_connected:
        log.error(f"Vault not connected — cannot write '{path}'.")
        return False
    try:
        kwargs: Dict[str, Any] = {} if cas is None else {"cas": cas}
        vault_instance.client.secrets.kv.v2.create_or_update_secret(
            path=path, mount_point=_MOUNT, secret=data, **kwargs
        )
        return True
    except Exception as exc:
        if cas is not None and _is_cas_conflict(exc):
            raise VaultCasConflict(f"Vault secret '{path}' changed concurrently") from exc
        log.error(f"Vault write failed for '{path}': {exc}")
        return False


def update_secret_dict(
    path: str,
    mutate: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Safe read-modify-write of ``lyndrix/{path}``; returns the stored dict.

    ``mutate`` receives a copy of the current content and either changes it in
    place (returning ``None``) or returns the new content. It may run more than
    once (CAS retry), so it must be free of side effects. Exceptions it raises
    propagate untouched and nothing is written.

    Raises :class:`VaultReadError` when the current content cannot be read (no
    write happens) and :class:`VaultWriteError` when the write fails or the CAS
    conflict does not resolve within a few attempts.
    """
    for attempt in range(1, _CAS_ATTEMPTS + 1):
        current, version = read_secret_strict(path)
        working = dict(current)
        result = mutate(working)
        new_data = working if result is None else dict(result)
        if new_data == current and version > 0:
            return new_data  # nothing to change — do not burn a KV version
        try:
            if not write_secret_dict(path, new_data, cas=version):
                raise VaultWriteError(f"Vault write failed for '{path}'")
            return new_data
        except VaultCasConflict:
            if attempt == _CAS_ATTEMPTS:
                raise
            log.info(f"Vault CAS conflict on '{path}' (attempt {attempt}) — re-reading.")
            # Jitter so two instances that collided do not collide again in step.
            time.sleep(random.uniform(0.02, 0.1) * attempt)
    raise VaultWriteError(f"Vault write failed for '{path}'")  # pragma: no cover - loop always returns/raises
