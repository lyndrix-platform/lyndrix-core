from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:VaultAPI")

vault_api_router = APIRouter(prefix="/api/vault", tags=["Vault"])


class VaultInitRequest(BaseModel):
    key: str


class VaultUnsealRequest(BaseModel):
    key: str


@vault_api_router.get("/status", summary="Vault status")
async def vault_status():
    """Returns Vault connectivity and initialization state. Public — no auth required."""
    from core.services import vault_instance
    try:
        initialized = vault_instance.client.sys.is_initialized()
        sealed = vault_instance.client.sys.is_sealed() if initialized else True
    except Exception as exc:
        return {
            "status": "error",
            "initialized": False,
            "sealed": True,
            "connected": False,
            "ui_state": vault_instance.ui_state,
            "error": str(exc),
        }
    return {
        "status": "ok",
        "initialized": initialized,
        "sealed": sealed,
        "connected": vault_instance.is_connected,
        "ui_state": vault_instance.ui_state,
    }


@vault_api_router.post("/init", summary="Initialize Vault")
async def vault_init(payload: VaultInitRequest):
    """
    Triggers Vault initialization via the event bus. Public — no auth possible
    before Vault is initialized. Use only during first-time setup.
    """
    from core.services import vault_instance
    try:
        initialized = vault_instance.client.sys.is_initialized()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Cannot reach Vault: {exc}")
    if initialized:
        raise HTTPException(status_code=400, detail="Vault is already initialized")

    bus.emit("vault:init_requested", {"key": payload.key})
    return {"status": "ok", "action": "init_requested"}


@vault_api_router.post("/unseal", summary="Unseal Vault")
async def vault_unseal(payload: VaultUnsealRequest):
    """
    Triggers Vault unseal via the event bus. Public — no auth possible
    while Vault is sealed. Use only during the unseal flow.
    """
    from core.services import vault_instance
    try:
        initialized = vault_instance.client.sys.is_initialized()
        sealed = vault_instance.client.sys.is_sealed()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Cannot reach Vault: {exc}")
    if not initialized:
        raise HTTPException(status_code=400, detail="Vault is not initialized")
    if not sealed:
        raise HTTPException(status_code=400, detail="Vault is already unsealed")

    bus.emit("vault:unseal_requested", {"key": payload.key})
    return {"status": "ok", "action": "unseal_requested"}
