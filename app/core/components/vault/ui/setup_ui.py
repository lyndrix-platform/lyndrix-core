from nicegui import ui
from core.bus import bus
from core.i18n import t
from ui.theme import apply_theme, UIStyles
from core.services import vault_instance
from ui.maintenance import attach_maintenance_overlay

from ..logic.crypto import MIN_MASTER_KEY_LENGTH

def render_setup_wizard():
    apply_theme(page_title="Setup")
    attach_maintenance_overlay()
    ui.query('body').style('background-color: #09090b;')

    with ui.column().classes('w-full h-screen items-center justify-center ' + UIStyles.AUTH_PAGE_BG):
        with ui.card().classes(UIStyles.AUTH_CARD):
            with ui.column().classes('items-center w-full gap-4'):
                ui.icon('auto_awesome', size='48px').classes('text-emerald-500 mb-2')
                ui.label(t("auth.setup.title")).classes('text-2xl font-bold tracking-tight')
                ui.label(t("auth.setup.subtitle")).classes(UIStyles.AUTH_SUBTITLE)

                master_key_input = ui.input(t("auth.setup.new_master_key")).props('type=password outlined dark').classes('w-full mb-2')
                status_label = ui.label('').classes('text-xs font-mono')

                def do_init():
                    if len(master_key_input.value or "") < MIN_MASTER_KEY_LENGTH:
                        ui.notify(
                            t("auth.setup.key_too_short", length=MIN_MASTER_KEY_LENGTH),
                            type="negative",
                        )
                        return
                    status_label.set_text(t("auth.setup.initializing"))
                    bus.emit("vault:init_requested", {"key": master_key_input.value})

                ui.button(t("auth.setup.submit"), on_click=do_init).classes(UIStyles.BUTTON_PRIMARY).props('unelevated')

    async def check_setup_status():
        if vault_instance.ui_state != "needs_init":
            setup_timer.cancel()
            ui.timer(0.5, lambda: ui.navigate.to('/'), once=True)

    setup_timer = ui.timer(1.0, check_setup_status)

    @bus.subscribe("vault:init_failed")
    def on_init_failed(payload):
        status_label.set_text(t("auth.setup.init_failed"))
