import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.bus import bus
from core.logger import get_logger

from ..logic.crypto import validate_master_key, WeakMasterKeyError
from ..logic.rate_limit import unseal_limiter

log = get_logger("Core:VaultAPI")

vault_api_router = APIRouter(prefix="/api/vault", tags=["Vault"])


class VaultInitRequest(BaseModel):
    key: str


class VaultUnsealRequest(BaseModel):
    key: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_rate_limit(request: Request) -> None:
    """Per-IP throttle for init/unseal to slow online brute-force of the master key."""
    retry = unseal_limiter.hit(_client_ip(request))
    if retry > 0:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": str(retry)},
        )


@vault_api_router.get("/status", summary="Vault status")
async def vault_status():
    """Returns Vault connectivity and initialization state. Public — no auth required."""
    from core.services import vault_instance
    try:
        initialized = await asyncio.to_thread(vault_instance.client.sys.is_initialized)
        sealed = (
            await asyncio.to_thread(vault_instance.client.sys.is_sealed)
            if initialized
            else True
        )
    except Exception as exc:
        # Do not leak Vault host/internals to unauthenticated callers; log detail server-side.
        log.warning(f"Vault status probe failed: {exc}")
        return {
            "status": "error",
            "initialized": False,
            "sealed": True,
            "connected": False,
            "ui_state": vault_instance.ui_state,
        }
    return {
        "status": "ok",
        "initialized": initialized,
        "sealed": sealed,
        "connected": vault_instance.is_connected,
        "ui_state": vault_instance.ui_state,
    }


@vault_api_router.post("/init", summary="Initialize Vault")
async def vault_init(payload: VaultInitRequest, request: Request):
    """
    Triggers Vault initialization via the event bus. Public — no auth possible
    before Vault is initialized. Use only during first-time setup.
    """
    from core.services import vault_instance
    _enforce_rate_limit(request)

    # Enforce master-key strength server-side so the API cannot set a weak key.
    try:
        validate_master_key(payload.key)
    except WeakMasterKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        initialized = await asyncio.to_thread(vault_instance.client.sys.is_initialized)
    except Exception as exc:
        log.warning(f"Cannot reach Vault during init: {exc}")
        raise HTTPException(status_code=503, detail="Cannot reach Vault")
    if initialized:
        raise HTTPException(status_code=400, detail="Vault is already initialized")

    bus.emit("vault:init_requested", {"key": payload.key})
    return {"status": "ok", "action": "init_requested"}


@vault_api_router.post("/unseal", summary="Unseal Vault")
async def vault_unseal(payload: VaultUnsealRequest, request: Request):
    """
    Triggers Vault unseal via the event bus. Public — no auth possible
    while Vault is sealed. Use only during the unseal flow.
    """
    from core.services import vault_instance
    _enforce_rate_limit(request)

    try:
        initialized = await asyncio.to_thread(vault_instance.client.sys.is_initialized)
        sealed = await asyncio.to_thread(vault_instance.client.sys.is_sealed)
    except Exception as exc:
        log.warning(f"Cannot reach Vault during unseal: {exc}")
        raise HTTPException(status_code=503, detail="Cannot reach Vault")
    if not initialized:
        raise HTTPException(status_code=400, detail="Vault is not initialized")
    if not sealed:
        raise HTTPException(status_code=400, detail="Vault is already unsealed")

    bus.emit("vault:unseal_requested", {"key": payload.key})
    return {"status": "ok", "action": "unseal_requested"}
