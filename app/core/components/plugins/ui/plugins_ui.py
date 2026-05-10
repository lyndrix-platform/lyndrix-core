import os

from nicegui import ui

from core.logger import get_logger, log_capture_buffer
from ui.theme import UIStyles

from ..logic.manager import module_manager
from ..logic.plugin_service import plugin_service

log = get_logger("UI:Plugins")

PLUGIN_DIALOG_CLASSES = (
    f'w-[calc(100vw-40px)] h-[calc(100vh-40px)] max-w-none '
    f'max-h-none p-[20px] flex flex-col {UIStyles.MODAL_CONTAINER}'
)


def _module_folder_name(module):
    if not module or not hasattr(module, '__file__'):
        return None
    return os.path.basename(os.path.dirname(module.__file__))


def _version_label(version: str) -> str:
    return version if version.startswith('v') else f'v{version}'


def _plugin_source_kind(manifest, folder_name: str, source_map: dict) -> str:
    if manifest.type == 'CORE':
        return 'core'
    return 'marketplace' if source_map.get(folder_name) or manifest.repo_url else 'local'


def _source_badge(source_kind: str):
    if source_kind == 'core':
        return 'Core', 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
    if source_kind == 'marketplace':
        return 'Marketplace', 'bg-sky-500/15 text-sky-300 border border-sky-500/30'
    return 'Local', 'bg-zinc-700/40 text-zinc-200 border border-zinc-600/60'


def _status_badge(status: str):
    mapping = {
        'active': ('Active', 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'),
        'disabled': ('Disabled', 'bg-zinc-700/40 text-zinc-200 border border-zinc-600/60'),
        'blocked': ('Blocked', 'bg-amber-500/15 text-amber-300 border border-amber-500/30'),
        'initializing': ('Loading', 'bg-indigo-500/15 text-indigo-300 border border-indigo-500/30'),
    }
    return mapping.get(status, ('Unknown', 'bg-zinc-700/40 text-zinc-200 border border-zinc-600/60'))


def _is_deleted_slot_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return (
        'slot belongs to has been deleted' in message
        or 'parent element this slot belongs to has been deleted' in message
    )


def _safe_notify(message: str, notify_type: str = 'info'):
    try:
        ui.notify(message, type=notify_type)
    except RuntimeError as exc:
        if not _is_deleted_slot_error(exc):
            raise
        log.info(f"Plugins UI: skipped notify after client teardown: {message}")


def render_plugins_page():
    """Renders the full plugin management page."""

    def collect_module_records():
        source_map = plugin_service.get_marketplace_source_map()
        records = []
        by_folder = {}

        for module_id, entry in module_manager.registry.items():
            manifest = entry['manifest']
            folder_name = _module_folder_name(entry.get('module'))
            source_url = source_map.get(folder_name) or manifest.repo_url
            source_kind = _plugin_source_kind(manifest, folder_name, source_map)
            record = {
                'module_id': module_id,
                'manifest': manifest,
                'entry': entry,
                'folder_name': folder_name,
                'source_url': source_url,
                'source_kind': source_kind,
            }
            records.append(record)
            if folder_name:
                by_folder[folder_name] = record
                if folder_name.startswith('lyndrix_'):
                    by_folder[folder_name[len('lyndrix_'):]] = record

        records.sort(key=lambda record: (0 if record['manifest'].type == 'CORE' else 1, record['manifest'].name.lower()))
        return records, by_folder

    async def populate_version_select(select, source_url: str, current_version: str = None, force_refresh: bool = False):
        if not source_url:
            return

        tags = await plugin_service.get_plugin_versions(source_url, force_refresh=force_refresh)
        options = ['latest'] + [tag for tag in tags if tag != 'latest']
        if current_version and current_version not in options:
            bare_version = current_version.lstrip('v')
            prefixed_version = current_version if current_version.startswith('v') else f'v{current_version}'
            if prefixed_version in options:
                current_version = prefixed_version
            elif bare_version in options:
                current_version = bare_version
            else:
                options.append(current_version)

        select.options = options
        select.value = current_version if current_version in options else options[0]
        select.update()

    async def uninstall_record(record):
        manifest = record['manifest']
        folder_name = record['folder_name']
        if manifest.type != 'PLUGIN' or not folder_name:
            _safe_notify('Core-Module konnen nicht deinstalliert werden.', 'warning')
            return

        if await plugin_service.uninstall_plugin(manifest.id, folder_name):
            try:
                await load_installed()
                await load_shop()
            except RuntimeError as exc:
                if not _is_deleted_slot_error(exc):
                    raise
                log.info(f"Plugins UI: skipped uninstall refresh for {manifest.name} after client teardown")
            _safe_notify(f'{manifest.name} wurde deinstalliert.', 'positive')
        else:
            _safe_notify(f'Deinstallation von {manifest.name} fehlgeschlagen.', 'negative')

    def open_logs(manifest):
        with ui.dialog() as log_dialog, ui.card().classes(f'w-full max-w-4xl h-[80vh] {UIStyles.MODAL_CONTAINER}'):
            with ui.row().classes('w-full justify-between items-center mb-4'):
                ui.label(f'Logs: {manifest.name}').classes('text-xl font-bold font-mono text-emerald-500')
                ui.button(icon='close', on_click=log_dialog.close).props('flat round dense')

            log_container = ui.scroll_area().classes('w-full flex-grow bg-black/50 rounded-xl p-4 font-mono text-xs')
            target_logger = f"Plugin:{manifest.name}" if manifest.type == 'PLUGIN' else f"Core:{manifest.name}"
            found_logs = [entry for entry in log_capture_buffer if entry[0] == target_logger]

            with log_container:
                if not found_logs:
                    ui.label('Keine Logs gefunden.').classes('text-zinc-600 italic')
                for _, level, message in found_logs:
                    color = 'text-red-500' if level in ['ERROR', 'CRITICAL'] else 'text-zinc-300'
                    ui.label(message).classes(f'{color} whitespace-pre-wrap mb-1')

        log_dialog.open()

    def open_settings(record):
        manifest = record['manifest']
        active = record['entry'].get('status') == 'active'

        with ui.dialog() as settings_dialog, ui.card().classes(PLUGIN_DIALOG_CLASSES):
            with ui.row().classes('w-full justify-between items-center mb-4 shrink-0'):
                ui.label(f'Settings: {manifest.name}').classes('text-xl font-bold font-mono text-emerald-500')
                ui.button(icon='close', on_click=settings_dialog.close).props('flat round dense')

            with ui.scroll_area().classes('w-full flex-grow pr-4'):
                with ui.column().classes('w-full gap-4'):
                    def toggle_plugin_inner(event):
                        module_manager.toggle_module(manifest.id, event.value)
                        if event.value:
                            _safe_notify(f'{manifest.name} aktiviert.', 'positive')
                        else:
                            _safe_notify(f'{manifest.name} deaktiviert.', 'warning')

                    ui.switch('Plugin Aktiviert', value=active, on_change=toggle_plugin_inner).props('color=emerald').classes('w-full')

                    entry = module_manager.registry.get(manifest.id)
                    if entry and entry.get('status') == 'active':
                        module = entry.get('module')
                        ctx = entry.get('context')
                        if module and hasattr(module, 'render_settings_ui'):
                            ui.separator().classes('bg-zinc-800 my-2')
                            ui.label('Konfiguration').classes('text-xs font-bold uppercase tracking-widest text-zinc-500')
                            try:
                                module.render_settings_ui(ctx)
                            except Exception as exc:
                                ui.label(f'Fehler in Plugin-UI: {exc}').classes('text-red-500 text-xs')

                    ui.separator().classes('bg-zinc-800')

                    if manifest.type == 'PLUGIN':
                        async def reload_plugin_inner():
                            try:
                                _safe_notify(f'Reloade {manifest.name}...', 'ongoing')
                                await module_manager.reload_module(manifest.id)
                                try:
                                    await load_installed()
                                    await load_shop()
                                    _safe_notify('Reload abgeschlossen.', 'positive')
                                    settings_dialog.close()
                                except RuntimeError as exc:
                                    if not _is_deleted_slot_error(exc):
                                        raise
                                    log.info(f"Plugins UI: skipped reload refresh for {manifest.name} after client teardown")
                            except Exception as exc:
                                _safe_notify(f'Fehler beim Reload: {exc}', 'negative')

                        async def delete_plugin_inner():
                            await uninstall_record(record)
                            settings_dialog.close()

                        ui.button('Plugin neu laden', icon='refresh', on_click=reload_plugin_inner).props('outline color=slate').classes('w-full')
                        ui.button('Deinstallieren', icon='delete', on_click=delete_plugin_inner).props('unelevated color=red').classes('w-full')

        settings_dialog.open()

    async def check_all_updates():
        notif = ui.notification('Prufe Marketplace-Daten neu...', spinner=True, timeout=0, type='ongoing', close_button=False)
        try:
            await load_installed()
            await load_shop(force_refresh=True)
        except RuntimeError as exc:
            if not _is_deleted_slot_error(exc):
                raise
            log.info('Plugins UI: skipped marketplace refresh after client teardown')
            notif.dismiss()
            return
        except Exception as exc:
            notif.dismiss()
            _safe_notify(f'Fehler beim Aktualisieren: {exc}', 'negative')
            return
        notif.dismiss()
        _safe_notify('Marketplace aktualisiert.', 'positive')

    with ui.column().classes('w-full max-w-7xl gap-6 mx-auto'):
        with ui.row().classes('w-full justify-between items-center'):
            with ui.column().classes('gap-0'):
                ui.label('Plugin Management').classes(UIStyles.TITLE_H2)
                ui.label('Installierte Module reagieren sofort, Marketplace-Daten laden in kleinen GitHub-freundlichen Batches.').classes(UIStyles.TEXT_MUTED)
            ui.button('Updates suchen', icon='update', on_click=check_all_updates).props('outline color=slate').tooltip('Aktualisiert Marketplace-Metadaten und Versionen bei Bedarf')

        with ui.tabs().classes(UIStyles.TAB_BAR) as tabs:
            tab_installed = ui.tab('Installiert', icon='extension')
            tab_shop = ui.tab('Marketplace', icon='shopping_bag')

        with ui.tab_panels(tabs, value=tab_installed).classes('w-full bg-transparent p-0 mt-4'):
            with ui.tab_panel(tab_installed).classes('p-0'):
                ui.label('Installierte Module').classes(UIStyles.TITLE_H3 + ' mb-4')
                installed_container = ui.grid(columns=3).classes('w-full gap-6')
                with installed_container:
                    ui.spinner('dots', size='lg').classes('col-span-3 mx-auto')

                async def load_installed(force_refresh=False):
                    del force_refresh
                    records, _ = collect_module_records()
                    installed_container.clear()

                    with installed_container:
                        if not records:
                            ui.label('Keine Plugins installiert.').classes('col-span-3 text-zinc-500 italic p-4')
                            return

                        for record in records:
                            manifest = record['manifest']
                            entry = record['entry']
                            source_label, source_classes = _source_badge(record['source_kind'])
                            status_label, status_classes = _status_badge(entry.get('status'))
                            current_version = manifest.version if record['source_kind'] == 'local' else _version_label(manifest.version)

                            with ui.card().classes(UIStyles.CARD_GLASS + ' flex flex-col overflow-hidden').style('padding: 0; flex-wrap: nowrap'):
                                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 via-sky-400 to-indigo-500')
                                with ui.column().classes('w-full flex-grow gap-4 p-5'):
                                    with ui.row().classes('w-full items-start justify-between gap-3'):
                                        with ui.row().classes('items-center gap-3 min-w-0'):
                                            ui.icon(manifest.icon, size='30px').classes('text-primary shrink-0')
                                            with ui.column().classes('gap-0 min-w-0'):
                                                ui.label(manifest.name).classes('font-bold text-lg leading-tight truncate')
                                                ui.label(f'by {manifest.author}').classes('text-[10px] text-zinc-500 uppercase tracking-widest')
                                        ui.label(manifest.type).classes('text-[9px] font-bold px-2 py-1 rounded-full bg-zinc-800/80 text-zinc-200 shrink-0')

                                    with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                                        ui.label(source_label).classes(f'text-[10px] font-bold px-2 py-1 rounded-full {source_classes}')
                                        ui.label(status_label).classes(f'text-[10px] font-bold px-2 py-1 rounded-full {status_classes}')
                                        if record['folder_name']:
                                            ui.label(record['folder_name']).classes('text-[10px] font-mono px-2 py-1 rounded-full bg-black/20 text-zinc-300')

                                    ui.label(manifest.description).classes('text-sm text-zinc-400 leading-relaxed min-h-[72px] flex-grow')

                                    with ui.row().classes('w-full items-center justify-between gap-3 pt-2 border-t border-zinc-800/60 flex-wrap'):
                                        with ui.row().classes('items-center gap-2 flex-wrap'):
                                            version_select = ui.select(options=[current_version], value=current_version).classes('w-32').props('dense options-dense outlined borderless')

                                            if record['source_url'] and record['source_kind'] == 'marketplace':
                                                ui.button(
                                                    icon='unfold_more',
                                                    on_click=lambda select=version_select, url=record['source_url'], version=current_version: populate_version_select(select, url, version)
                                                ).props('flat round size=sm color=slate').tooltip('Verfugbare Versionen laden')

                                                async def run_update(select=version_select, url=record['source_url'], name=manifest.name):
                                                    _safe_notify(f'Installiere {select.value} fur {name}...', 'ongoing')
                                                    if await plugin_service.install_plugin(url, version=select.value, upgrade=True):
                                                        try:
                                                            await load_installed()
                                                            await load_shop()
                                                        except RuntimeError as exc:
                                                            if not _is_deleted_slot_error(exc):
                                                                raise
                                                            log.info(f"Plugins UI: skipped update refresh for {name} after client teardown")
                                                        _safe_notify(f'{name} aktualisiert.', 'positive')
                                                    else:
                                                        _safe_notify(f'Update von {name} fehlgeschlagen.', 'negative')

                                                ui.button(icon='cloud_download', on_click=run_update).props('flat round size=sm color=warning').tooltip('Ausgewahlte Version installieren')

                                        with ui.row().classes('items-center gap-1 ml-auto'):
                                            ui.button(icon='article', on_click=lambda manifest=manifest: open_logs(manifest)).props('flat round size=sm color=slate').tooltip('Logs anzeigen')
                                            ui.button(icon='settings', on_click=lambda record=record: open_settings(record)).props('flat round size=sm color=slate').tooltip('Konfiguration offnen')
                                            if manifest.type == 'PLUGIN':
                                                ui.button(icon='delete', on_click=lambda record=record: uninstall_record(record)).props('flat round size=sm color=red').tooltip('Schnell deinstallieren')

                ui.timer(0.1, load_installed, once=True)

            with ui.tab_panel(tab_shop).classes('p-0'):
                ui.label('Marketplace').classes(UIStyles.TITLE_H3 + ' mb-4')
                search = ui.input(placeholder='Plugins suchen...').props('outlined dark dense icon=search').classes('w-full mb-6')
                shop_container = ui.grid(columns=3).classes('w-full gap-6')
                with shop_container:
                    ui.spinner('dots', size='lg').classes('col-span-3 mx-auto')

                async def load_shop(force_refresh=False):
                    shop_container.clear()
                    with shop_container:
                        ui.spinner('dots', size='lg').classes('col-span-3 mx-auto')

                    plugins = await plugin_service.fetch_marketplace_data(force_refresh=force_refresh)
                    _, registry_by_folder = collect_module_records()
                    query = (search.value or '').strip().lower()

                    filtered_plugins = [
                        plugin for plugin in plugins
                        if not query or query in ' '.join([
                            str(plugin.get('name', '')),
                            str(plugin.get('description', '')),
                            str(plugin.get('author', '')),
                        ]).lower()
                    ]

                    shop_container.clear()
                    with shop_container:
                        if not filtered_plugins:
                            ui.label('Keine Plugins im Marketplace gefunden.').classes('col-span-3 text-center text-zinc-500')
                            return

                        for plugin in filtered_plugins:
                            repo_safe = plugin.get('repo_safe')
                            repo_aliases = plugin.get('repo_aliases') or ([repo_safe] if repo_safe else [])
                            installed_record = None
                            for alias in repo_aliases:
                                installed_record = registry_by_folder.get(alias)
                                if installed_record:
                                    break
                            installed_manifest = installed_record['manifest'] if installed_record else None
                            installed_version = _version_label(installed_manifest.version) if installed_manifest else 'latest'

                            with ui.card().classes(UIStyles.CARD_GLASS + ' flex flex-col overflow-hidden').style('padding: 0; flex-wrap: nowrap'):
                                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-sky-400 via-cyan-400 to-emerald-400')
                                with ui.column().classes('w-full flex-grow gap-4 p-5'):
                                    with ui.row().classes('w-full justify-between items-start gap-3'):
                                        with ui.column().classes('gap-1 min-w-0'):
                                            ui.label(plugin['name']).classes(UIStyles.TITLE_H3 + ' truncate')
                                            ui.label(f"by {plugin['author']}").classes('text-[10px] text-zinc-500 uppercase tracking-widest')
                                        with ui.row().classes('items-center gap-2 shrink-0'):
                                            with ui.row().classes('items-center gap-1 text-amber-500 bg-amber-500/10 px-2 py-1 rounded-full'):
                                                ui.icon('star', size='14px')
                                                ui.label(str(plugin['stars'])).classes('text-xs font-mono')
                                            ui.button(icon='menu_book', on_click=lambda url=plugin['url']: ui.navigate.to(url, new_tab=True)).props('flat dense size=sm color=slate').tooltip('Readme offnen')

                                    with ui.row().classes('items-center gap-2 flex-wrap'):
                                        ui.label('Marketplace').classes('text-[10px] font-bold px-2 py-1 rounded-full bg-sky-500/15 text-sky-300 border border-sky-500/30')
                                        if installed_record:
                                            status_label, status_classes = _status_badge(installed_record['entry'].get('status'))
                                            ui.label('Installiert').classes('text-[10px] font-bold px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30')
                                            ui.label(status_label).classes(f'text-[10px] font-bold px-2 py-1 rounded-full {status_classes}')
                                        if plugin.get('metadata_source') == 'fallback':
                                            ui.label('Cached/Fallback').classes('text-[10px] font-bold px-2 py-1 rounded-full bg-zinc-700/40 text-zinc-200 border border-zinc-600/60')

                                    ui.label(plugin['description']).classes('text-sm text-zinc-400 leading-relaxed min-h-[72px] flex-grow')

                                    with ui.row().classes('w-full items-center justify-between gap-3 pt-2 border-t border-zinc-800/60 flex-wrap'):
                                        with ui.row().classes('items-center gap-2 flex-wrap'):
                                            version_select = ui.select(
                                                options=[installed_version if installed_record else 'latest'],
                                                value=installed_version if installed_record else 'latest',
                                            ).classes('w-32').props('dense options-dense outlined borderless')

                                            ui.button(
                                                icon='unfold_more',
                                                on_click=lambda select=version_select, url=plugin['clone_url'], version=installed_version if installed_record else None: populate_version_select(select, url, version)
                                            ).props('flat round size=sm color=slate').tooltip('Verfugbare Versionen laden')

                                        with ui.row().classes('items-center gap-2 ml-auto'):
                                            if installed_record:
                                                async def upgrade_plugin(select=version_select, url=plugin['clone_url'], name=plugin['name']):
                                                    _safe_notify(f'Update {name} auf {select.value}...', 'ongoing')
                                                    if await plugin_service.install_plugin(url, version=select.value, upgrade=True):
                                                        try:
                                                            await load_installed()
                                                            await load_shop()
                                                        except RuntimeError as exc:
                                                            if not _is_deleted_slot_error(exc):
                                                                raise
                                                            log.info(f"Plugins UI: skipped marketplace update refresh for {name} after client teardown")
                                                        _safe_notify(f'{name} aktualisiert.', 'positive')
                                                    else:
                                                        _safe_notify(f'Update von {name} fehlgeschlagen.', 'negative')

                                                ui.button(icon='sync', on_click=upgrade_plugin).props('unelevated size=sm color=warning rounded').tooltip('Version andern')
                                                ui.button(icon='delete', on_click=lambda record=installed_record: uninstall_record(record)).props('unelevated size=sm color=red rounded').tooltip('Schnell deinstallieren')
                                            else:
                                                async def install_plugin_handler(select=version_select, url=plugin['clone_url'], name=plugin['name']):
                                                    _safe_notify(f'Installiere {name} ({select.value})...', 'ongoing')
                                                    if await plugin_service.install_plugin(url, version=select.value):
                                                        try:
                                                            await load_installed()
                                                            await load_shop()
                                                        except RuntimeError as exc:
                                                            if not _is_deleted_slot_error(exc):
                                                                raise
                                                            log.info(f"Plugins UI: skipped install refresh for {name} after client teardown")
                                                        _safe_notify(f'{name} installiert und aktiviert.', 'positive')
                                                    else:
                                                        _safe_notify(f'Installation von {name} fehlgeschlagen.', 'negative')

                                                ui.button('Install', icon='download', on_click=install_plugin_handler).props('unelevated size=sm color=primary rounded')

                search.on('update:model-value', lambda _: ui.timer(0.05, load_shop, once=True))
                ui.timer(0.1, load_shop, once=True)


def render_plugin_manager():
    """Called from the settings page."""
    render_plugins_page()
