from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from nicegui import ui, app

from core.logger import get_logger
from .login_ui import render_login_page

log = get_logger("Core:AuthRoutes")


def register_auth_routes():
    """Register NiceGUI UI pages for the auth component."""

    @ui.page('/login')
    def login_page():
        render_login_page()

    @ui.page('/auth/complete')
    async def auth_complete(state: str = ''):
        """
        Landing page after a successful OIDC redirect.
        Reads the pending AuthResult from the OIDC provider, sets the NiceGUI session,
        and forwards the user to /dashboard.
        """
        from core.components.auth.logic.providers.registry import provider_registry

        if not state:
            ui.navigate.to('/login')
            return

        oidc_provider = provider_registry.get("oidc")
        if not oidc_provider:
            log.warning("AUTH: /auth/complete reached but no OIDC provider is registered.")
            ui.navigate.to('/login')
            return

        result = oidc_provider.consume_result(state)
        if not result:
            log.warning(f"AUTH: /auth/complete — no pending result for state '{state[:8]}…'.")
            ui.notify('SSO-Anmeldung fehlgeschlagen oder abgelaufen.', type='negative')
            ui.navigate.to('/login')
            return

        app.storage.user.update({
            'authenticated': True,
            'username': result.username,
            'full_name': result.full_name,
            'roles': result.roles,
            'email': result.email,
            'auth_provider': result.provider,
            'extra_permissions': result.extra_permissions,
        })
        log.info(f"AUTH: OIDC session established for '{result.username}'.")
        ui.navigate.to('/dashboard')


def register_oidc_fastapi_routes(fastapi_app: FastAPI):
    """Register the FastAPI HTTP route that handles the OIDC authorization-code callback."""

    @fastapi_app.get('/auth/callback/oidc')
    async def oidc_callback(
        code: str = '',
        state: str = '',
        error: str = '',
        error_description: str = '',
    ):
        """
        OIDC Authorization Code callback endpoint.

        Configure this URL as the redirect URI in your OIDC provider
        (e.g. Authentik → Application → Redirect URIs):
          https://<lyndrix-host>/auth/callback/oidc
        """
        from core.components.auth.logic.providers.registry import provider_registry

        if error:
            log.warning(f"AUTH:OIDC: Provider returned error '{error}': {error_description}")
            return RedirectResponse('/login?error=sso_error')

        if not code or not state:
            log.warning("AUTH:OIDC: Callback reached without code or state parameter.")
            return RedirectResponse('/login?error=invalid_callback')

        oidc_provider = provider_registry.get("oidc")
        if not oidc_provider:
            log.error("AUTH:OIDC: Callback received but no OIDC provider is registered.")
            return RedirectResponse('/login?error=no_oidc_provider')

        result = await oidc_provider.exchange_code(code, state)
        if not result:
            return RedirectResponse('/login?error=auth_failed')

        # Hand off to the NiceGUI /auth/complete page to set the session
        return RedirectResponse(f'/auth/complete?state={state}')
