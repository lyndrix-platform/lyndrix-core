import psutil
from nicegui import ui
from core.logger import get_logger

# 1. Globale UI Tools
from ui.theme import UIStyles 
from ui.layout import main_layout

# 2. Komponentenspezifische Imports
from core.components.auth.ui.auth_cards import render_user_settings_card
from core.components.plugins.ui.plugins_ui import render_plugin_manager 

# 3. Manager importieren, um echte Plugins zu laden
from core.components.plugins.logic.manager import module_manager

from core.services import vault_instance

log = get_logger("UI:Settings")

async def render_settings_page():
    """Rendert das komplette Settings-Dashboard."""
    
    with ui.column().classes('w-full max-w-5xl mx-auto p-4 gap-6'):
        # --- Header ---
        with ui.row().classes('w-full items-center justify-between mb-4'):
            ui.label('⚙️ Systemeinstellungen').classes(UIStyles.TITLE_H1)
            ui.label('Lyndrix Core Engine').classes(UIStyles.TEXT_MUTED + ' uppercase tracking-widest text-sm')
        
        ui.separator().classes('bg-zinc-800')

        # --- Tabs Navigation ---
        with ui.tabs().classes('w-full justify-start border-b border-zinc-800 text-zinc-400') as tabs:
            tab_profile = ui.tab('Profil', icon='person').classes('capitalize')
            tab_system  = ui.tab('System', icon='settings').classes('capitalize')
            tab_plugins = ui.tab('Plugins', icon='extension').classes('capitalize')
            tab_info    = ui.tab('Info', icon='info').classes('capitalize')
            
        # --- Tab Inhalte ---
        with ui.tab_panels(tabs, value=tab_profile).classes('w-full bg-transparent p-0 mt-6'):
            
            # 1. PROFIL
            with ui.tab_panel(tab_profile).classes('p-0 gap-6'):
                render_user_settings_card()

            # 2. SYSTEM
            with ui.tab_panel(tab_system).classes('p-0 gap-6'):
                with ui.card().classes(UIStyles.CARD_GLASS + ' w-full p-6'):
                    ui.label('Grundeinstellungen').classes(UIStyles.TITLE_H3 + ' mb-4')
                    ui.input('System Name', value='Lyndrix Core').classes('w-full max-w-md mb-4').props('outlined dark')
                    
                    ui.separator().classes('my-4 bg-zinc-800')
                    
                    ui.label('API Konfiguration').classes(UIStyles.TITLE_H3 + ' mb-2')
                    ui.label('Ein GitHub Token verhindert Rate-Limits beim Laden des Marketplaces.').classes(UIStyles.TEXT_MUTED + ' text-sm mb-4')
                    
                    current_token = ""
                    if vault_instance.is_connected:
                        try:
                            resp = vault_instance.client.secrets.kv.v2.read_secret_version(path="core/settings", mount_point="lyndrix")
                            current_token = resp['data']['data'].get('github_token', '')
                        except Exception: pass
                    
                    gh_token_input = ui.input('GitHub API Token', value=current_token, password=True).classes('w-full max-w-md').props('outlined dark')
                    
                    def save_system_settings():
                        if vault_instance.is_connected:
                            try:
                                current_data = {}
                                try:
                                    resp = vault_instance.client.secrets.kv.v2.read_secret_version(path="core/settings", mount_point="lyndrix")
                                    current_data = resp['data']['data']
                                except Exception: pass
                                
                                current_data['github_token'] = gh_token_input.value
                                vault_instance.client.secrets.kv.v2.create_or_update_secret(path="core/settings", mount_point="lyndrix", secret=current_data)
                                ui.notify('Systemeinstellungen gespeichert', type='positive')
                            except Exception as e:
                                ui.notify(f'Fehler beim Speichern: {e}', type='negative')
                                
                    ui.button('Speichern', on_click=save_system_settings, icon='save').classes('mt-4').props('color=primary unelevated')

            # 3. PLUGINS (DYNAMISCH)
            with ui.tab_panel(tab_plugins).classes('p-0 gap-6'):
                # 3.1 Der Installer (GitHub Download)
                render_plugin_manager()

            # 4. SYSTEM INFO
            with ui.tab_panel(tab_info).classes('p-0 gap-6'):
                with ui.card().classes(UIStyles.CARD_GLASS + ' w-full p-6'):
                    ui.label('System Status').classes(UIStyles.TITLE_H3 + ' mb-4')
                    ui.label('Version: v3.0 Stable').classes(UIStyles.TEXT_MUTED)
                    ui.label('Status: Alle Systeme online').classes('text-green-500 mt-4')