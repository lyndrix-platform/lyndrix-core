"""Public i18n catalog endpoint.

Serves the nested, i18next-shaped translation catalog to the React UI (and any
future external frontend) over HTTP.  Unauthenticated and reachable during boot
(the login / vault-setup pages must localize before auth) — mirrors the
unauthenticated style of ``vault_api.py``.

Two endpoints:

    GET /api/i18n/locales        -> available locales + catalog metadata
    GET /api/i18n/{locale}       -> nested resources for a locale (with ?ns / ?v)

Only the ``I18NEXT_NAMESPACES`` allowlist (``ui``, ``settings``) is served by
default, so the ``%{name}`` NiceGUI dialect never reaches an i18next client.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from config import settings
from core.i18n import (
    available_locales,
    client_namespaces,
    get_catalog,
    get_catalog_version,
)
from core.logger import get_logger

log = get_logger("Core:i18nAPI")

i18n_router = APIRouter(prefix="/api/i18n", tags=["i18n"])


@i18n_router.get("/locales", summary="List supported locales and catalog metadata")
def list_locales() -> dict:
    return {
        "default": settings.DEFAULT_LOCALE,
        "supported": available_locales(),
        "client_namespaces": client_namespaces(),
        "version": get_catalog_version(),
    }


@i18n_router.get("/{locale}", summary="Get the translation catalog for a locale")
def get_locale_catalog(
    locale: str,
    ns: str | None = Query(None, description="Comma-separated namespaces"),
    v: str | None = Query(None, description="Client-known catalog version"),
) -> Response:
    if locale not in available_locales():
        raise HTTPException(status_code=404, detail="unsupported locale")

    version = get_catalog_version()
    if v is not None and v == version:
        return Response(status_code=304)

    allowed = set(client_namespaces())
    if ns:
        # Silently drop any namespace not on the i18next allowlist so NiceGUI
        # %{name}-dialect namespaces (core, auth, …) never reach React clients.
        ns_list = [n.strip() for n in ns.split(",") if n.strip() in allowed]
        if not ns_list:
            ns_list = client_namespaces()
    else:
        ns_list = client_namespaces()

    resources = get_catalog(locale, ns_list)
    return JSONResponse(
        content={"locale": locale, "version": version, "resources": resources},
        headers={"ETag": f'"{version}"', "Cache-Control": "no-cache"},
    )
