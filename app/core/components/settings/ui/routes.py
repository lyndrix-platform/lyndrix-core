from nicegui import ui
from ui.layout import main_layout  # global layout from /app/ui/
from .settings_ui import render_settings_page  # local UI from this folder


def register_settings_routes():
    # Privileged page: editing settings exposes Vault secrets, auth-provider
    # config and the system API key — require the edit permission, not just login.
    @ui.page('/settings')
    @main_layout('Settings', permission='feature:settings.edit')
    async def settings_page():
        await render_settings_page()