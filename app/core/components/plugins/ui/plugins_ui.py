import os

from nicegui import ui

from core.i18n import t
from core.logger import get_logger, log_capture_buffer
from ui.theme import UIStyles

from ..logic.manager import module_manager
from ..logic.plugin_service import plugin_service
from ..logic.custom_repo_service import custom_repo_service
from ..logic import git_providers

log = get_logger("UI:Plugins")

PLUGIN_DIALOG_CLASSES = (
    f'w-[calc(100vw-24px)] h-[calc(100dvh-24px)] sm:w-[calc(100vw-40px)] '
    f'sm:h-[calc(100vh-40px)] max-w-none max-h-none p-4 sm:p-[20px] '
    f'flex flex-col {UIStyles.MODAL_CONTAINER}'
)

# Responsive card grid: one column on phones, two on tablets, three on wide screens.
PLUGIN_GRID_CLASSES = 'grid w-full gap-4 sm:gap-6 grid-cols-1 sm:grid-cols-2 xl:grid-cols-3'


def _module_folder_name(module):
    if not module or not hasattr(module, '__file__'):
        return None
    return os.path.basename(os.path.dirname(module.__file__))


def _version_label(version: str) -> str:
    return version if version.startswith('v') else f'v{version}'


def _normalize_repo_url(url: str) -> str:
    """Host/owner/repo key for matching, ignoring scheme, .git and trailing slash."""
    if not url:
        return ''
    key = url.strip().lower().rstrip('/')
    for prefix in ('https://', 'http://', 'git@'):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    key = key.replace(':', '/')  # scp-style git@host:owner/repo
    if key.endswith('.git'):
        key = key[:-4]
    return key


def _plugin_source_kind(manifest, folder_name: str, source_map: dict) -> str:
    if manifest.type == 'CORE':
        return 'core'
    return 'marketplace' if source_map.get(folder_name) or manifest.repo_url else 'local'


def _source_badge(source_kind: str):
    if source_kind == 'core':
        return t('plugins.source.core'), 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
    if source_kind == 'marketplace':
        return t('plugins.source.marketplace'), 'bg-sky-500/15 text-sky-300 border border-sky-500/30'
    return t('plugins.source.local'), 'bg-zinc-700/40 text-zinc-200 border border-zinc-600/60'


def _status_badge(status: str):
    mapping = {
        'active': (t('plugins.status.active'), 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'),
        'disabled': (t('plugins.status.disabled'), 'bg-zinc-700/40 text-zinc-200 border border-zinc-600/60'),
        'blocked': (t('plugins.status.blocked'), 'bg-amber-500/15 text-amber-300 border border-amber-500/30'),
        'initializing': (t('plugins.status.initializing'), 'bg-indigo-500/15 text-indigo-300 border border-indigo-500/30'),
    }
    return mapping.get(status, (t('plugins.status.unknown'), 'bg-zinc-700/40 text-zinc-200 border border-zinc-600/60'))


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
            # Also index by normalized repo URL so a marketplace entry matches its
            # installed plugin even when the repo name differs from the folder name
            # (e.g. repo 'monitoring' installed as 'state_monitoring').
            repo_url_key = _normalize_repo_url(manifest.repo_url)
            if repo_url_key:
                by_folder.setdefault(repo_url_key, record)

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
            _safe_notify(t('plugins.notify.no_uninstall_core'), 'warning')
            return

        if await plugin_service.uninstall_plugin(manifest.id, folder_name):
            try:
                await load_installed()
                await load_shop()
            except RuntimeError as exc:
                if not _is_deleted_slot_error(exc):
                    raise
                log.info(f"Plugins UI: skipped uninstall refresh for {manifest.name} after client teardown")
            _safe_notify(t('plugins.notify.uninstalled', name=manifest.name), 'positive')
        else:
            _safe_notify(t('plugins.notify.uninstall_failed', name=manifest.name), 'negative')

    def open_logs(manifest):
        with ui.dialog() as log_dialog, ui.card().classes(f'w-[calc(100vw-24px)] max-w-4xl h-[85dvh] sm:h-[80vh] {UIStyles.MODAL_CONTAINER}'):
            with ui.row().classes('w-full justify-between items-center mb-4'):
                ui.label(f"{t('plugins.installed.logs_title')}: {manifest.name}").classes('text-xl font-bold font-mono text-emerald-500')
                ui.button(icon='close', on_click=log_dialog.close).props('flat round dense')

            log_container = ui.scroll_area().classes('w-full flex-grow bg-black/50 rounded-xl p-4 font-mono text-xs')
            target_logger = f"Plugin:{manifest.name}" if manifest.type == 'PLUGIN' else f"Core:{manifest.name}"
            found_logs = [entry for entry in log_capture_buffer if entry[0] == target_logger]

            with log_container:
                if not found_logs:
                    ui.label(t('plugins.installed.no_logs')).classes(UIStyles.TEXT_HINT + ' italic')
                for _, level, message in found_logs:
                    color = 'text-red-500' if level in ['ERROR', 'CRITICAL'] else 'text-slate-700 dark:text-zinc-300'
                    ui.label(message).classes(f'{color} whitespace-pre-wrap mb-1')

        log_dialog.open()

    def open_settings(record):
        manifest = record['manifest']
        active = record['entry'].get('status') == 'active'

        with ui.dialog() as settings_dialog, ui.card().classes(PLUGIN_DIALOG_CLASSES):
            with ui.row().classes('w-full justify-between items-center mb-4 shrink-0'):
                ui.label(f"{t('plugins.installed.settings')}: {manifest.name}").classes('text-xl font-bold font-mono text-emerald-500')
                ui.button(icon='close', on_click=settings_dialog.close).props('flat round dense')

            with ui.scroll_area().classes('w-full flex-grow pr-4'):
                with ui.column().classes('w-full gap-4'):
                    def toggle_plugin_inner(event):
                        module_manager.toggle_module(manifest.id, event.value)
                        if event.value:
                            _safe_notify(t('plugins.notify.activated', name=manifest.name), 'positive')
                        else:
                            _safe_notify(t('plugins.notify.deactivated', name=manifest.name), 'warning')

                    ui.switch(t('plugins.installed.toggle_active'), value=active, on_change=toggle_plugin_inner).props('color=emerald').classes('w-full')

                    entry = module_manager.registry.get(manifest.id)
                    if entry and entry.get('status') == 'active':
                        module = entry.get('module')
                        ctx = entry.get('context')
                        if module and hasattr(module, 'render_settings_ui'):
                            ui.separator().classes('bg-slate-200 dark:bg-white/10 my-2')
                            ui.label(t('plugins.installed.settings')).classes(UIStyles.LABEL_MINI)
                            try:
                                module.render_settings_ui(ctx)
                            except Exception as exc:
                                ui.label(t('plugins.notify.reload_failed', error=str(exc))).classes('text-red-500 text-xs')

                    ui.separator().classes('bg-slate-200 dark:bg-white/10')

                    if manifest.type == 'PLUGIN':
                        async def reload_plugin_inner():
                            try:
                                _safe_notify(t('plugins.notify.reloading', name=manifest.name), 'ongoing')
                                await module_manager.reload_module(manifest.id)
                                try:
                                    await load_installed()
                                    await load_shop()
                                    _safe_notify(t('plugins.notify.reloaded'), 'positive')
                                    settings_dialog.close()
                                except RuntimeError as exc:
                                    if not _is_deleted_slot_error(exc):
                                        raise
                                    log.info(f"Plugins UI: skipped reload refresh for {manifest.name} after client teardown")
                            except Exception as exc:
                                _safe_notify(t('plugins.notify.reload_failed', error=str(exc)), 'negative')

                        async def delete_plugin_inner():
                            await uninstall_record(record)
                            settings_dialog.close()

                        ui.button(t('plugins.installed.reload'), icon='refresh', on_click=reload_plugin_inner).props('outline color=slate').classes('w-full')
                        ui.button(t('plugins.installed.uninstall'), icon='delete', on_click=delete_plugin_inner).props('unelevated color=red').classes('w-full')

        settings_dialog.open()

    async def check_all_updates():
        notif = ui.notification(t('plugins.notify.checking'), spinner=True, timeout=0, type='ongoing', close_button=False)
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
            _safe_notify(t('plugins.notify.check_failed', error=str(exc)), 'negative')
            return
        notif.dismiss()
        _safe_notify(t('plugins.notify.checked'), 'positive')

    with ui.column().classes('w-full max-w-7xl gap-6 mx-auto'):
        with ui.row().classes('w-full flex-col gap-3 sm:flex-row sm:justify-between sm:items-center'):
            with ui.column().classes('gap-0 min-w-0'):
                ui.label(t('plugins.title')).classes(UIStyles.TITLE_H2)
                ui.label(t('plugins.subtitle')).classes(UIStyles.TEXT_MUTED)
            ui.button(t('plugins.check_updates'), icon='update', on_click=check_all_updates).props('outline color=slate').classes('w-full sm:w-auto').tooltip(t('plugins.notify.checking'))

        with ui.tabs().classes(UIStyles.TAB_BAR) as tabs:
            tab_installed = ui.tab(t('plugins.tabs.installed'), icon='extension')
            tab_shop = ui.tab(t('plugins.tabs.marketplace'), icon='shopping_bag')

        with ui.tab_panels(tabs, value=tab_installed).classes('w-full bg-transparent p-0 mt-4'):
            with ui.tab_panel(tab_installed).classes('p-0'):
                ui.label(t('plugins.installed.title')).classes(UIStyles.TITLE_H3 + ' mb-4')
                installed_container = ui.element('div').classes(PLUGIN_GRID_CLASSES)
                with installed_container:
                    ui.spinner('dots', size='lg').classes('col-span-full mx-auto')

                async def load_installed(force_refresh=False):
                    del force_refresh
                    records, _ = collect_module_records()
                    installed_container.clear()

                    with installed_container:
                        if not records:
                            ui.label(t('plugins.installed.empty')).classes('col-span-full p-4 ' + UIStyles.TEXT_MUTED + ' italic')
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
                                                ui.label(f'by {manifest.author}').classes(UIStyles.LABEL_MINI)
                                        ui.label(manifest.type).classes('text-[9px] font-bold px-2 py-1 rounded-full bg-zinc-800/80 text-zinc-200 shrink-0')

                                    with ui.row().classes('w-full items-center gap-2 flex-wrap'):
                                        ui.label(source_label).classes(f'text-[10px] font-bold px-2 py-1 rounded-full {source_classes}')
                                        ui.label(status_label).classes(f'text-[10px] font-bold px-2 py-1 rounded-full {status_classes}')
                                        if record['folder_name']:
                                            ui.label(record['folder_name']).classes('text-[10px] font-mono px-2 py-1 rounded-full bg-black/20 text-zinc-300')

                                    ui.label(manifest.description).classes(UIStyles.TEXT_MUTED + ' leading-relaxed min-h-[72px] flex-grow')

                                    with ui.row().classes('w-full items-center justify-between gap-3 pt-2 border-t border-slate-200 dark:border-white/10 flex-wrap'):
                                        with ui.row().classes('items-center gap-2 flex-wrap grow'):
                                            # Pre-populate from collection-cached versions so the dropdown
                                            # is usable without contacting GitHub.
                                            known = plugin_service.get_known_versions(record['source_url']) if record['source_url'] else []
                                            version_options = [current_version] + [v for v in known if v != current_version]
                                            version_select = ui.select(options=version_options, value=current_version).classes('w-full sm:w-32').props('dense options-dense outlined borderless')

                                            if record['source_url'] and record['source_kind'] == 'marketplace':
                                                ui.button(
                                                    icon='unfold_more',
                                                    on_click=lambda select=version_select, url=record['source_url'], version=current_version: populate_version_select(select, url, version, force_refresh=True)
                                                ).props('flat round size=sm color=slate').tooltip(t('plugins.installed.load_versions'))

                                                async def run_update(select=version_select, url=record['source_url'], name=manifest.name):
                                                    _safe_notify(t('plugins.notify.updating', name=name, version=select.value), 'ongoing')
                                                    if await plugin_service.install_plugin(url, version=select.value, upgrade=True):
                                                        try:
                                                            await load_installed()
                                                            await load_shop()
                                                        except RuntimeError as exc:
                                                            if not _is_deleted_slot_error(exc):
                                                                raise
                                                            log.info(f"Plugins UI: skipped update refresh for {name} after client teardown")
                                                        _safe_notify(t('plugins.notify.updated', name=name), 'positive')
                                                    else:
                                                        _safe_notify(t('plugins.notify.update_failed', name=name), 'negative')

                                                ui.button(icon='cloud_download', on_click=run_update).props('flat round size=sm color=warning').tooltip(t('plugins.installed.install_version'))

                                        with ui.row().classes('items-center gap-1 ml-auto'):
                                            ui.button(icon='article', on_click=lambda manifest=manifest: open_logs(manifest)).props('flat round size=sm color=slate').tooltip(t('plugins.installed.logs'))
                                            ui.button(icon='settings', on_click=lambda record=record: open_settings(record)).props('flat round size=sm color=slate').tooltip(t('plugins.installed.settings'))
                                            if manifest.type == 'PLUGIN':
                                                ui.button(icon='delete', on_click=lambda record=record: uninstall_record(record)).props('flat round size=sm color=red').tooltip(t('plugins.installed.uninstall'))

                ui.timer(0.1, load_installed, once=True)

            with ui.tab_panel(tab_shop).classes('p-0'):
                with ui.row().classes('w-full flex-col gap-3 sm:flex-row sm:justify-between sm:items-center mb-4'):
                    ui.label(t('plugins.marketplace.title')).classes(UIStyles.TITLE_H3)
                    ui.button(
                        t('plugins.marketplace.add_repo'),
                        icon='add_link',
                        on_click=lambda: open_add_repo_dialog(),
                    ).props('outline color=primary').classes('w-full sm:w-auto')
                search = ui.input(placeholder=t('plugins.marketplace.search_placeholder')).props('outlined dark dense icon=search').classes('w-full mb-6')
                shop_container = ui.element('div').classes(PLUGIN_GRID_CLASSES)
                with shop_container:
                    ui.spinner('dots', size='lg').classes('col-span-full mx-auto')

                async def load_shop(force_refresh=False):
                    shop_container.clear()
                    with shop_container:
                        ui.spinner('dots', size='lg').classes('col-span-full mx-auto')

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
                            ui.label(t('plugins.marketplace.empty')).classes('col-span-full text-center ' + UIStyles.TEXT_MUTED)
                            return

                        for plugin in filtered_plugins:
                            repo_safe = plugin.get('repo_safe')
                            repo_aliases = plugin.get('repo_aliases') or ([repo_safe] if repo_safe else [])
                            # Match by folder-name aliases first, then by normalized repo URL.
                            match_keys = list(repo_aliases)
                            for url_key in (plugin.get('clone_url'), plugin.get('url')):
                                norm = _normalize_repo_url(url_key)
                                if norm:
                                    match_keys.append(norm)
                            installed_record = None
                            for alias in match_keys:
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
                                            ui.label(f"by {plugin['author']}").classes(UIStyles.LABEL_MINI)
                                        with ui.row().classes('items-center gap-2 shrink-0'):
                                            with ui.row().classes('items-center gap-1 text-amber-500 bg-amber-500/10 px-2 py-1 rounded-full'):
                                                ui.icon('star', size='14px')
                                                ui.label(str(plugin['stars'])).classes('text-xs font-mono')
                                            ui.button(icon='menu_book', on_click=lambda url=plugin['url']: ui.navigate.to(url, new_tab=True)).props('flat dense size=sm color=slate').tooltip(t('plugins.marketplace.open_readme'))
                                            if plugin.get('metadata_source') == 'custom':
                                                ui.button(
                                                    icon='link_off',
                                                    on_click=lambda cid=plugin.get('custom_id'), name=plugin['name']: remove_custom_repo(cid, name),
                                                ).props('flat dense size=sm color=red').tooltip(t('plugins.marketplace.remove_repo'))

                                    with ui.row().classes('items-center gap-2 flex-wrap'):
                                        if plugin.get('metadata_source') == 'custom':
                                            ui.label(t('plugins.source.custom')).classes('text-[10px] font-bold px-2 py-1 rounded-full bg-violet-500/15 text-violet-300 border border-violet-500/30')
                                            ui.label(str(plugin.get('provider', '')).upper()).classes('text-[10px] font-mono px-2 py-1 rounded-full bg-black/20 text-zinc-300')
                                        else:
                                            ui.label(t('plugins.source.marketplace')).classes('text-[10px] font-bold px-2 py-1 rounded-full bg-sky-500/15 text-sky-300 border border-sky-500/30')
                                        if installed_record:
                                            status_label, status_classes = _status_badge(installed_record['entry'].get('status'))
                                            ui.label(t('plugins.tabs.installed')).classes('text-[10px] font-bold px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30')
                                            ui.label(status_label).classes(f'text-[10px] font-bold px-2 py-1 rounded-full {status_classes}')
                                        if plugin.get('metadata_source') == 'fallback':
                                            ui.label(t('plugins.marketplace.cached')).classes('text-[10px] font-bold px-2 py-1 rounded-full bg-zinc-700/40 text-zinc-200 border border-zinc-600/60')

                                    # Collection descriptions are often empty; fall back to the
                                    # installed plugin's manifest description, then a placeholder.
                                    card_description = (
                                        (plugin.get('description') or '').strip()
                                        or (getattr(installed_manifest, 'description', '') or '').strip()
                                        or t('plugins.marketplace.no_description')
                                    )
                                    ui.label(card_description).classes(UIStyles.TEXT_MUTED + ' leading-relaxed min-h-[72px] flex-grow')

                                    with ui.row().classes('w-full items-center justify-between gap-3 pt-2 border-t border-slate-200 dark:border-white/10 flex-wrap'):
                                        with ui.row().classes('items-center gap-2 flex-wrap grow'):
                                            # Versions shipped by the collection pre-populate the picker
                                            # (no GitHub call); the refresh button fetches newer ones.
                                            default_version = installed_version if installed_record else 'latest'
                                            shop_versions = plugin.get('versions') or ['latest']
                                            shop_options = [default_version] + [v for v in shop_versions if v != default_version]
                                            version_select = ui.select(
                                                options=shop_options,
                                                value=default_version,
                                            ).classes('w-full sm:w-32').props('dense options-dense outlined borderless')

                                            ui.button(
                                                icon='unfold_more',
                                                on_click=lambda select=version_select, url=plugin['clone_url'], version=installed_version if installed_record else None: populate_version_select(select, url, version, force_refresh=True)
                                            ).props('flat round size=sm color=slate').tooltip(t('plugins.installed.load_versions'))

                                        with ui.row().classes('items-center gap-2 ml-auto'):
                                            if installed_record:
                                                async def upgrade_plugin(select=version_select, url=plugin['clone_url'], name=plugin['name']):
                                                    _safe_notify(t('plugins.notify.updating', name=name, version=select.value), 'ongoing')
                                                    if await plugin_service.install_plugin(url, version=select.value, upgrade=True):
                                                        try:
                                                            await load_installed()
                                                            await load_shop()
                                                        except RuntimeError as exc:
                                                            if not _is_deleted_slot_error(exc):
                                                                raise
                                                            log.info(f"Plugins UI: skipped marketplace update refresh for {name} after client teardown")
                                                        _safe_notify(t('plugins.notify.updated', name=name), 'positive')
                                                    else:
                                                        _safe_notify(t('plugins.notify.update_failed', name=name), 'negative')

                                                ui.button(icon='sync', on_click=upgrade_plugin).props('unelevated size=sm color=warning rounded').tooltip(t('plugins.marketplace.update'))
                                                ui.button(icon='delete', on_click=lambda record=installed_record: uninstall_record(record)).props('unelevated size=sm color=red rounded').tooltip(t('plugins.installed.uninstall'))
                                            else:
                                                async def install_plugin_handler(select=version_select, url=plugin['clone_url'], name=plugin['name']):
                                                    _safe_notify(t('plugins.notify.installing', name=name, version=select.value), 'ongoing')
                                                    if await plugin_service.install_plugin(url, version=select.value):
                                                        try:
                                                            await load_installed()
                                                            await load_shop()
                                                        except RuntimeError as exc:
                                                            if not _is_deleted_slot_error(exc):
                                                                raise
                                                            log.info(f"Plugins UI: skipped install refresh for {name} after client teardown")
                                                        _safe_notify(t('plugins.notify.installed', name=name), 'positive')
                                                    else:
                                                        _safe_notify(t('plugins.notify.install_failed', name=name), 'negative')

                                                ui.button(t('plugins.marketplace.install'), icon='download', on_click=install_plugin_handler).props('unelevated size=sm color=primary rounded')

                async def remove_custom_repo(custom_id, name):
                    if not custom_id:
                        return
                    if custom_repo_service.delete(custom_id):
                        try:
                            await load_shop(force_refresh=True)
                        except RuntimeError as exc:
                            if not _is_deleted_slot_error(exc):
                                raise
                            log.info('Plugins UI: skipped shop refresh after custom repo removal')
                        _safe_notify(t('plugins.marketplace.repo_removed', name=name), 'positive')
                    else:
                        _safe_notify(t('plugins.marketplace.repo_remove_failed', name=name), 'negative')

                def open_add_repo_dialog():
                    with ui.dialog() as dialog, ui.card().classes(f'w-[calc(100vw-24px)] max-w-lg {UIStyles.MODAL_CONTAINER}'):
                        with ui.row().classes('w-full justify-between items-center mb-2'):
                            ui.label(t('plugins.marketplace.add_repo_title')).classes('text-xl font-bold font-mono text-emerald-500')
                            ui.button(icon='close', on_click=dialog.close).props('flat round dense')

                        ui.label(t('plugins.marketplace.add_repo_hint')).classes(UIStyles.TEXT_MUTED + ' text-sm mb-2')

                        url_input = ui.input(
                            label=t('plugins.marketplace.repo_url'),
                            placeholder='https://github.com/owner/lyndrix-plugin-example',
                        ).props('outlined dark dense').classes('w-full')
                        provider_hint = ui.label('').classes(UIStyles.LABEL_MINI + ' h-4')

                        def _update_hint():
                            value = (url_input.value or '').strip()
                            if not value:
                                provider_hint.text = ''
                                return
                            try:
                                provider = git_providers.detect_provider(value)
                                provider_hint.text = t('plugins.marketplace.detected_provider', provider=provider.upper())
                            except Exception:
                                provider_hint.text = ''
                        url_input.on('update:model-value', lambda _: _update_hint())

                        name_input = ui.input(
                            label=t('plugins.marketplace.repo_name_optional'),
                        ).props('outlined dark dense').classes('w-full')
                        token_input = ui.input(
                            label=t('plugins.marketplace.repo_token_optional'),
                            password=True,
                        ).props('outlined dark dense').classes('w-full')
                        ui.label(t('plugins.marketplace.repo_token_hint')).classes(UIStyles.LABEL_MINI)

                        async def submit():
                            url = (url_input.value or '').strip()
                            if not url:
                                _safe_notify(t('plugins.marketplace.repo_url_required'), 'warning')
                                return
                            try:
                                custom_repo_service.create(
                                    repo_url=url,
                                    name=name_input.value,
                                    token=token_input.value,
                                )
                            except ValueError as exc:
                                _safe_notify(str(exc), 'negative')
                                return
                            except Exception as exc:
                                _safe_notify(t('plugins.marketplace.repo_add_failed', error=str(exc)), 'negative')
                                return
                            dialog.close()
                            try:
                                await load_shop(force_refresh=True)
                            except RuntimeError as exc:
                                if not _is_deleted_slot_error(exc):
                                    raise
                                log.info('Plugins UI: skipped shop refresh after custom repo add')
                            _safe_notify(t('plugins.marketplace.repo_added'), 'positive')

                        with ui.row().classes('w-full justify-end gap-2 mt-4'):
                            ui.button(t('plugins.marketplace.cancel'), on_click=dialog.close).props('flat color=slate')
                            ui.button(t('plugins.marketplace.add'), icon='add_link', on_click=submit).props('unelevated color=primary')

                    dialog.open()

                search.on('update:model-value', lambda _: ui.timer(0.05, load_shop, once=True))
                ui.timer(0.1, load_shop, once=True)


def render_plugin_manager():
    """Called from the settings page."""
    render_plugins_page()
