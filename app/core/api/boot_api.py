"""Public boot status endpoint.

``GET /api/boot`` is readable before Vault, the database and IAM are up — the
boot interceptor lets every ``/api`` path through — so the React shell can show
which core dependencies are already online and which are still coming. The
payload is built from in-memory flags only (see ``BootService.snapshot``) and
carries no error text, so it is safe for anonymous callers and cheap to poll.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

boot_router = APIRouter(prefix="/api/boot", tags=["Boot"])


@boot_router.get("", summary="Boot status (public)")
async def boot_status():
    from core.services import boot_service

    return JSONResponse(boot_service.snapshot(), headers={"Cache-Control": "no-store"})
