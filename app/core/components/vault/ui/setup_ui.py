from nicegui import ui
from core.bus import bus
from ui.theme import apply_theme, UIStyles
# FIX: Pfad korrigiert auf die neue Fassade
from core.services import vault_instance
from ui.maintenance import attach_maintenance_overlay

def render_setup_wizard():
    apply_theme(page_title="Setup")
    attach_maintenance_overlay() 
    ui.query('body').style('background-color: #09090b;') 
    
    with ui.column().classes('w-full h-screen items-center justify-center ' + UIStyles.AUTH_PAGE_BG):
        with ui.card().classes(UIStyles.AUTH_CARD):
            with ui.column().classes('items-center w-full gap-4'):
                ui.icon('auto_awesome', size='48px').classes('text-emerald-500 mb-2')
                ui.label("System Initialisierung").classes('text-2xl font-bold tracking-tight')
                ui.label("Bitte lege einen sicheren Master-Key fest.").classes(UIStyles.AUTH_SUBTITLE)
                
                master_key_input = ui.input("Neuer Master-Key").props('type=password outlined dark').classes('w-full mb-2')
                status_label = ui.label('').classes('text-xs font-mono')
                
                def do_init():
                    if len(master_key_input.value) < 8: 
                        ui.notify("Der Key muss mindestens 8 Zeichen lang sein!", type="negative")
                        return
                    status_label.set_text('⏳ Initialisiere Vault...')
                    bus.emit("vault:init_requested", {"key": master_key_input.value})
                    
                ui.button("Vault Initialisieren", on_click=do_init).classes(UIStyles.BUTTON_PRIMARY).props('unelevated')

    async def check_setup_status():
        if vault_instance.ui_state != "needs_init":
            setup_timer.cancel() 
            ui.timer(0.5, lambda: ui.navigate.to('/'), once=True)

    setup_timer = ui.timer(1.0, check_setup_status)