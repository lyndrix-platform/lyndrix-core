import os
import asyncio
from nicegui import ui, run
from ui.theme import UIStyles
from core.logger import get_logger, log_capture_buffer

# Wir importieren unseren Service für den Download
from ..logic.plugin_service import plugin_service
# Wir importieren den Manager, um die Liste der installierten Module zu bekommen
from ..logic.manager import module_manager

log = get_logger("UI:Plugins")

def render_plugins_page():
    """Rendert die komplette Plugin-Verwaltungsseite (Installer + Liste)."""
    
    async def check_all_updates():
        ui.notify('Prüfe auf Updates...', type='ongoing')
        await load_installed(force_refresh=True)
        await load_shop(force_refresh=True)
        ui.notify('Update-Prüfung abgeschlossen.', type='positive')

    with ui.column().classes('w-full max-w-7xl gap-6 mx-auto'):
        # Header
        with ui.row().classes('w-full justify-between items-center'):
            with ui.column().classes('gap-0'):
                ui.label('Plugin Management').classes('text-2xl font-bold dark:text-white')
                ui.label('Erweitere Lyndrix mit Community-Modulen.').classes(UIStyles.TEXT_MUTED)
            ui.button('Updates suchen', icon='update', on_click=check_all_updates).props('outline color=slate').tooltip('Prüft global auf neue Versionen')
        
        # Tabs
        with ui.tabs().classes('w-full justify-start border-b border-zinc-800 text-zinc-400') as tabs:
            tab_installed = ui.tab('Installiert', icon='extension')
            tab_shop = ui.tab('Marketplace', icon='shopping_bag')

        with ui.tab_panels(tabs, value=tab_installed).classes('w-full bg-transparent p-0 mt-4'):
            
            # --- TAB 1: INSTALLIERTE PLUGINS ---
            with ui.tab_panel(tab_installed).classes('p-0'):
                ui.label('Installierte Module').classes('text-lg font-bold mb-4')

                installed_container = ui.grid(columns=3).classes('w-full gap-6')
                
                async def load_installed(force_refresh=False):
                    with installed_container:
                        ui.spinner('dots', size='lg').classes('col-span-3 mx-auto')
                        
                    manifests = module_manager.get_manifests()
                    marketplace_plugins = await plugin_service.fetch_marketplace_data(force_refresh=force_refresh)
                    
                    repo_map = {}
                    for p in marketplace_plugins:
                        try:
                            u, r = plugin_service._extract_repo_info(p['clone_url'])
                            repo_map[r.replace("-", "_")] = p['clone_url']
                        except:
                            pass

                    installed_github_urls = []
                    for m in manifests:
                        entry = module_manager.registry.get(m.id, {})
                        mod = entry.get("module")
                        folder_name = os.path.basename(os.path.dirname(mod.__file__)) if mod and hasattr(mod, '__file__') else None
                        gurl = repo_map.get(folder_name) if folder_name else None
                        if gurl: installed_github_urls.append(gurl)
                        
                    tag_tasks = [plugin_service.get_plugin_versions(u, force_refresh=force_refresh) for u in installed_github_urls]
                    tags_results = await asyncio.gather(*tag_tasks, return_exceptions=True)
                    url_to_tags = {u: ["latest"] + (res if isinstance(res, list) else []) for u, res in zip(installed_github_urls, tags_results)}

                    installed_container.clear()
                    
                    with installed_container:
                        if not manifests:
                            ui.label("Keine Plugins installiert.").classes('col-span-3 text-zinc-500 italic p-4')
                            return

                        for m in manifests:
                            # Status prüfen
                            entry = module_manager.registry.get(m.id, {})
                            is_active = entry.get("status") == "active"
                            mod = entry.get("module")
                            folder_name = None
                            if mod and hasattr(mod, '__file__'):
                                folder_name = os.path.basename(os.path.dirname(mod.__file__))
                            github_url = repo_map.get(folder_name) if folder_name else None
                            
                            with ui.card().classes(UIStyles.CARD_GLASS + ' flex flex-col justify-between h-full'):
                                # Header Karte
                                with ui.row().classes('w-full items-start justify-between mb-2'):
                                    with ui.row().classes('items-center gap-3'):
                                        ui.icon(m.icon, size='32px').classes('text-primary')
                                        with ui.column().classes('gap-0'):
                                            ui.label(m.name).classes('font-bold text-lg leading-tight')
                                            ui.label(f'by {m.author}').classes('text-[10px] text-zinc-500 uppercase tracking-widest')
                                    
                                    badge_color = 'bg-amber-500/20 text-amber-500' if m.type == "CORE" else 'bg-emerald-500/20 text-emerald-500'
                                    ui.label(m.type).classes(f'text-[9px] font-bold px-2 py-1 rounded-full {badge_color}')

                            ui.label(m.description).classes('text-xs text-zinc-400 leading-relaxed line-clamp-3 mb-4 flex-grow')
                            
                            def open_logs(manifest=m):
                                with ui.dialog() as log_dialog, ui.card().classes('w-full max-w-4xl h-[80vh] bg-zinc-950 border border-zinc-800'):
                                    with ui.row().classes('w-full justify-between items-center mb-4'):
                                        ui.label(f'Logs: {manifest.name}').classes('text-xl font-bold font-mono text-emerald-500')
                                        ui.button(icon='close', on_click=log_dialog.close).props('flat round dense')
                                    
                                    log_container = ui.scroll_area().classes('w-full flex-grow bg-black/50 rounded-xl p-4 font-mono text-xs')
                                    
                                    # Logs filtern
                                    target_logger = f"Plugin:{manifest.name}" if manifest.type == "PLUGIN" else f"Core:{manifest.name}"
                                    found_logs = [entry for entry in log_capture_buffer if entry[0] == target_logger]
                                    
                                    with log_container:
                                        if not found_logs:
                                            ui.label('Keine Logs gefunden.').classes('text-zinc-600 italic')
                                        for logger_name, level, msg in found_logs:
                                            color = 'text-red-500' if level in ['ERROR', 'CRITICAL'] else 'text-zinc-300'
                                            ui.label(msg).classes(f'{color} whitespace-pre-wrap mb-1')
                                    
                                    log_dialog.open()

                            def open_settings(manifest=m, active=is_active):
                                # UPDATED CARD CLASSES: Full width/height calc, no max width, flex-col to allow inner scrolling, 20px padding
                                with ui.dialog() as settings_dialog, ui.card().classes('w-[calc(100vw-40px)] h-[calc(100vh-40px)] max-w-none max-h-none bg-zinc-950 border border-zinc-800 p-[20px] flex flex-col'):
                                    with ui.row().classes('w-full justify-between items-center mb-4 shrink-0'):
                                        ui.label(f'Settings: {manifest.name}').classes('text-xl font-bold font-mono text-emerald-500')
                                        ui.button(icon='close', on_click=settings_dialog.close).props('flat round dense')
                                    
                                    # UPDATED SCROLL AREA: flex-grow instead of max-h-[70vh] so it scales perfectly with the massive card
                                    with ui.scroll_area().classes('w-full flex-grow pr-4'):
                                        with ui.column().classes('w-full gap-4'):
                                            # 1. Status Switch
                                            def toggle_plugin_inner(e):
                                                module_manager.toggle_module(manifest.id, e.value)
                                                if e.value:
                                                    ui.notify(f'{manifest.name} aktiviert.', type='positive')
                                                else:
                                                    ui.notify(f'{manifest.name} deaktiviert.', type='warning')
                                            
                                            ui.switch('Plugin Aktiviert', value=active, on_change=toggle_plugin_inner).props('color=emerald').classes('w-full')
                                            
                                            # --- PLUGIN SPECIFIC SETTINGS ---
                                            entry = module_manager.registry.get(manifest.id)
                                            if entry and entry.get("status") == "active":
                                                mod = entry.get("module")
                                                ctx = entry.get("context")
                                                
                                                if mod and hasattr(mod, 'render_settings_ui'):
                                                    ui.separator().classes('bg-zinc-800 my-2')
                                                    ui.label('Konfiguration').classes('text-xs font-bold uppercase tracking-widest text-zinc-500')
                                                    try:
                                                        mod.render_settings_ui(ctx)
                                                    except Exception as e:
                                                        ui.label(f"Fehler in Plugin-UI: {str(e)}").classes('text-red-500 text-xs')
                                            
                                            ui.separator().classes('bg-zinc-800')

                                            # 2. Actions (Nur für Plugins, nicht Core)
                                            if manifest.type == "PLUGIN":
                                                async def reload_plugin_inner():
                                                    try:
                                                        ui.notify(f'Reloade {manifest.name}...', type='ongoing')
                                                        await module_manager.reload_module(manifest.id)
                                                        ui.notify('Reload abgeschlossen.', type='positive')
                                                        settings_dialog.close()
                                                        ui.navigate.to('/settings')
                                                    except Exception as e:
                                                        ui.notify(f'Fehler beim Reload: {str(e)}', type='negative')

                                                ui.button('Plugin neu laden', icon='refresh', on_click=reload_plugin_inner).props('outline color=slate').classes('w-full')
                                                
                                                async def delete_plugin_inner():
                                                    mod_entry = module_manager.registry.get(manifest.id, {})
                                                    mod = mod_entry.get("module")
                                                    if mod:
                                                        import os
                                                        folder_name = os.path.basename(os.path.dirname(mod.__file__))
                                                        # The service will emit an event that the manager will catch.
                                                        await plugin_service.uninstall_plugin(manifest.id, folder_name)
                                                        ui.notify(f'Plugin {folder_name} uninstalled. Please reload the page.', type='positive')
                                                        settings_dialog.close()
                                                    else:
                                                        ui.notify('Fehler: Modul nicht geladen. Bitte erst aktivieren.', type='warning')

                                                ui.button('Deinstallieren', icon='delete', color='red', on_click=delete_plugin_inner).props('unelevated').classes('w-full')

                                    settings_dialog.open()

                            # Actions Footer (Clean Left/Right Split)
                            with ui.row().classes('w-full mt-auto pt-4 border-t border-zinc-800/50 items-center justify-between'):
                                
                                # LEFT SIDE: Version / Update Selector / Local Tag
                                with ui.row().classes('items-center gap-2'):
                                    if m.type == "PLUGIN":
                                        if github_url:
                                            update_area = ui.row().classes('items-center gap-2')
                                            with update_area:
                                                async def check_updates(manifest=m, g_url=github_url, area=update_area, f_name=folder_name):
                                                    area.clear()
                                                    with area:
                                                        ui.spinner('dots', size='sm', color='primary')
                                                    
                                                    tags = await plugin_service.get_plugin_versions(g_url)
                                                    
                                                    area.clear()
                                                    with area:
                                                        if not tags:
                                                            ui.label(f"v{manifest.version}").classes('text-xs font-mono text-zinc-500')
                                                            return
                                                        
                                                        target_val = manifest.version
                                                        if target_val not in tags and f"v{target_val}" in tags:
                                                            target_val = f"v{target_val}"
                                                        elif target_val not in tags and tags:
                                                            target_val = tags[0]
                                                            
                                                        version_select = ui.select(options=tags, value=target_val).classes('w-28').props('dense options-dense outlined borderless')
                                                        
                                                        async def run_update(ver, gurl=g_url):
                                                            ui.notify(f"Installiere {ver}...", type="ongoing")
                                                            async def _bg_task():
                                                                if await plugin_service.install_plugin(gurl, version=ver, upgrade=True):
                                                                    ui.notify("Update erfolgreich! UI wird neu geladen...", type="positive")
                                                                else:
                                                                    ui.notify("Fehler beim Update.", type="negative")
                                                            asyncio.create_task(_bg_task())

                                                        ui.button(icon='cloud_download', on_click=lambda v=version_select: run_update(v.value)).props('flat round size=sm color=warning').tooltip("Version installieren")

                                            ui.button(f"v{m.version}", on_click=check_updates).props('flat dense size=sm color=slate').tooltip("Updates suchen")
                                        else:
                                            ui.label('Local').classes('text-xs font-mono text-zinc-500 bg-zinc-800/50 px-2 py-1 rounded').tooltip('Local Installation')
                                    else:
                                        ui.label(f'v{m.version}').classes('text-xs font-mono text-zinc-500')

                                # RIGHT SIDE: Action Buttons
                                with ui.row().classes('items-center gap-1'):
                                    ui.button(icon='article', on_click=open_logs).props('flat round size=sm color=slate').tooltip('Logs anzeigen')
                                    ui.button(icon='settings', on_click=open_settings).props('flat round size=sm color=slate').tooltip('Konfiguration öffnen')

                ui.timer(0.1, load_installed, once=True)

            # --- TAB 2: MARKETPLACE ---
            with ui.tab_panel(tab_shop).classes('p-0'):
                
                # Search Bar
                search = ui.input(placeholder='Plugins suchen...').props('outlined dark dense icon=search').classes('w-full mb-6')
                
                # Async Loader Container
                shop_container = ui.grid(columns=3).classes('w-full gap-6')
                
                async def load_shop(force_refresh=False):
                    with shop_container:
                        ui.spinner('dots', size='lg').classes('col-span-3 mx-auto')
                    
                    plugins = await plugin_service.fetch_marketplace_data(force_refresh=force_refresh)
                    
                    # Fetch available versions for all marketplace plugins concurrently
                    tag_tasks = [plugin_service.get_plugin_versions(p['clone_url'], force_refresh=force_refresh) for p in plugins]
                    tags_results = await asyncio.gather(*tag_tasks, return_exceptions=True)
                    for i, p in enumerate(plugins):
                        res = tags_results[i]
                        p['tags'] = ["latest"] + (res if isinstance(res, list) else [])
                    
                    shop_container.clear()
                    
                    with shop_container:
                        if not plugins:
                            ui.label("Keine Plugins im Marketplace gefunden.").classes('col-span-3 text-center text-zinc-500')
                            return

                        for p in plugins:
                            with ui.card().classes(UIStyles.CARD_GLASS + ' flex flex-col justify-between h-full'):
                                with ui.column().classes('gap-1'):
                                    with ui.row().classes('w-full justify-between items-start'):
                                        ui.label(p['name']).classes('font-bold text-lg')
                                        with ui.row().classes('items-center gap-2'):
                                            with ui.row().classes('items-center gap-1 text-amber-500'):
                                                ui.icon('star', size='14px')
                                                ui.label(str(p['stars'])).classes('text-xs font-mono')
                                            ui.button(icon='menu_book', on_click=lambda u=p['url']: ui.navigate.to(u, new_tab=True)).props('flat dense size=sm color=slate').tooltip('Readme öffnen')
                                    
                                    ui.label(f"by {p['author']}").classes('text-[10px] text-zinc-500 uppercase tracking-widest mb-2')
                                    ui.label(p['description']).classes('text-sm text-zinc-400 leading-relaxed line-clamp-3')

                                # Check if plugin is already installed
                                repo_safe = p['clone_url'].rstrip('/').split('/')[-1]
                                if repo_safe.endswith('.git'): repo_safe = repo_safe[:-4]
                                repo_safe = repo_safe.replace("-", "_")
                                
                                installed_version = None
                                for e in module_manager.registry.values():
                                    if "module" in e and hasattr(e["module"], '__file__'):
                                        if os.path.basename(os.path.dirname(e["module"].__file__)) == repo_safe:
                                            installed_version = e["manifest"].version
                                            break
                                
                                is_installed = installed_version is not None

                                with ui.row().classes('w-full mt-4 pt-4 border-t border-zinc-800/50 items-center justify-between'):
                                    if is_installed:
                                        target_val = installed_version
                                        if target_val not in p['tags'] and f"v{target_val}" in p['tags']:
                                            target_val = f"v{target_val}"
                                        elif target_val not in p['tags']:
                                            p['tags'].append(target_val)
                                        
                                        with ui.row().classes('items-center gap-2'):
                                            version_select = ui.select(options=p['tags'], value=target_val).classes('w-28').props('dense options-dense outlined borderless')
                                            
                                            latest_release = p['tags'][1] if len(p['tags']) > 1 else None
                                            if latest_release and latest_release.lstrip('v') != installed_version.lstrip('v'):
                                                ui.badge(f"Neu: {latest_release}", color="warning").classes('text-[10px] px-1 py-0')

                                        def create_upgrade_handler(url, vs_element):
                                            async def handler():
                                                v = vs_element.value
                                                ui.notify(f"Update {url} auf v{v}...", type='ongoing')
                                                async def _bg_task():
                                                    if await plugin_service.install_plugin(url, version=v, upgrade=True):
                                                        ui.notify("Update erfolgreich! UI wird neu geladen...", type="positive")
                                                    else:
                                                        ui.notify("Fehler beim Update.", type="negative")
                                                asyncio.create_task(_bg_task())
                                            return handler
                                            
                                        with ui.row().classes('items-center gap-2'):
                                            ui.label('Installiert').classes('text-[10px] font-bold text-emerald-500 uppercase tracking-widest hidden xl:block')
                                            ui.button(icon='sync', on_click=create_upgrade_handler(p['clone_url'], version_select)).props('unelevated size=sm color=warning rounded').tooltip('Version ändern')
                                    else:
                                        version_select = ui.select(options=p['tags'], value="latest").classes('w-28').props('dense options-dense outlined borderless')
                                        
                                        def create_install_handler(url, vs_element):
                                            async def handler():
                                                v = vs_element.value
                                                ui.notify(f"Installiere {url} (v{v})...", type='ongoing')
                                                if await plugin_service.install_plugin(url, version=v):
                                                    ui.notify("Installiert! Bitte UI neu laden.", type='positive')
                                                else:
                                                    ui.notify("Fehler bei Installation.", type='negative')
                                            return handler

                                        ui.button('Install', icon='download', on_click=create_install_handler(p['clone_url'], version_select)).props('unelevated size=sm color=primary rounded')

                # Trigger Load
                ui.timer(0.1, load_shop, once=True)

def render_plugin_manager():
    """Wird vom Settings-Tab aufgerufen. Wir leiten einfach auf die Hauptseite um oder rendern inline."""
    render_plugins_page()