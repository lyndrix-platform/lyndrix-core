import sys
import os
import platform
import psutil
from nicegui import ui

from config import settings
from core.i18n import t
from core.logger import get_logger
from ui.theme import UIStyles
from version import __version__, __codename__, __release_date__, get_uptime

from core.components.auth.ui.auth_cards import render_user_settings_card
from core.components.auth.ui.auth_settings_card import render_auth_settings_card
from core.components.auth.ui.groups_card import render_groups_card
from core.components.auth.ui.permissions_card import render_permissions_card
from core.components.plugins.ui.plugins_ui import render_plugin_manager
from core.components.plugins.logic.manager import module_manager
from core.services import vault_instance, db_instance

log = get_logger("UI:Settings")


def _kv_row(label: str, value: str, mono: bool = False, value_cls: str = ''):
    """Renders a labeled key/value row inside a column container."""
    with ui.row().classes('w-full items-center justify-between py-2 border-b border-zinc-800/40 last:border-0'):
        ui.label(label).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold shrink-0')
        cls = 'text-sm font-mono' if mono else 'text-sm'
        cls += f' {value_cls}' if value_cls else ' text-zinc-100'
        ui.label(value).classes(cls)

async def render_settings_page():
    """Renders the complete Settings dashboard."""

    with ui.column().classes('w-full max-w-5xl mx-auto gap-6'):
        # ── Page Header ─────────────────────────────────────────────────────
        with ui.row().classes('w-full items-center gap-4'):
            ui.element('div').classes('h-12 w-1 rounded-full bg-gradient-to-b from-indigo-400 to-sky-400')
            with ui.column().classes('gap-0'):
                ui.label(t('core.settings.page.title')).classes(UIStyles.TITLE_H2)
                ui.label(t('core.settings.page.subtitle')).classes(UIStyles.TEXT_MUTED + ' uppercase tracking-widest text-xs')

        ui.separator().classes('bg-zinc-800/60')

        # ── Tabs ────────────────────────────────────────────────────────────
        with ui.tabs().classes(UIStyles.TAB_BAR) as tabs:
            tab_profile  = ui.tab(t('core.settings.tabs.profile'),      icon='person')
            tab_system   = ui.tab(t('core.settings.tabs.system'),       icon='tune')
            tab_auth     = ui.tab(t('core.settings.tabs.auth'),         icon='security')
            tab_groups   = ui.tab(t('core.settings.tabs.groups'),       icon='group')
            tab_perms    = ui.tab(t('core.settings.tabs.permissions'),  icon='verified_user')
            tab_plugins  = ui.tab(t('core.settings.tabs.plugins'),      icon='extension')
            tab_info     = ui.tab(t('core.settings.tabs.info'),         icon='info')

        with ui.tab_panels(tabs, value=tab_profile).classes('w-full bg-transparent p-0 mt-4'):

            # ── PROFIL ──────────────────────────────────────────────────────
            with ui.tab_panel(tab_profile).classes('p-0'):
                render_user_settings_card()

            # ── SYSTEM ──────────────────────────────────────────────────────
            with ui.tab_panel(tab_system).classes('p-0'):
                with ui.column().classes('w-full gap-4'):

                    # Application Config
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-indigo-400 via-sky-400 to-cyan-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('tune', size='18px').classes('text-indigo-400')
                                ui.label(t('core.settings.system.application')).classes(UIStyles.TITLE_H3)
                            env_cls = 'text-amber-400 font-bold' if settings.ENV_TYPE == 'dev' else 'text-emerald-400 font-bold'
                            _kv_row(t('core.settings.system.app_name'), settings.APP_NAME)
                            _kv_row(t('core.settings.system.environment'), settings.ENV_TYPE.upper(), value_cls=env_cls)
                            _kv_row(t('core.settings.system.port'), str(settings.PORT), mono=True)
                            _kv_row(t('core.settings.system.log_level'), settings.LOG_LEVEL, mono=True)
                            ui.separator().classes('my-2 bg-zinc-800/40')
                            with ui.row().classes('w-full items-center justify-between gap-4'):
                                with ui.column().classes('gap-0'):
                                    ui.label(t('core.settings.system.log_level_session')).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold')
                                    ui.label(t('core.settings.system.log_level_env_hint')).classes('text-xs text-zinc-600')
                                log_select = ui.select(
                                    options=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                                    value=settings.LOG_LEVEL,
                                ).classes('w-40').props('dense options-dense outlined')

                            def apply_log_level():
                                import logging
                                logging.getLogger().setLevel(getattr(logging, log_select.value, logging.INFO))
                                ui.notify(t('core.settings.system.log_level_applied', level=log_select.value), type='positive')

                            ui.button(t('core.settings.system.apply'), icon='check', on_click=apply_log_level).props('unelevated size=sm color=primary').classes('mt-2 self-end')

                    # Plugin Reconciliation
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 via-teal-400 to-sky-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('extension', size='18px').classes('text-emerald-400')
                                ui.label(t('core.settings.system.plugin_reconciliation')).classes(UIStyles.TITLE_H3)
                            _kv_row(
                                t('core.settings.system.auto_update_on_boot'),
                                t('core.settings.system.yes') if settings.LYNDRIX_PLUGINS_AUTO_UPDATE else t('core.settings.system.no'),
                            )
                            _kv_row(
                                t('core.settings.system.desired_plugins_env'),
                                settings.LYNDRIX_PLUGINS_DESIRED or t('core.settings.system.none_configured'),
                                mono=True,
                            )
                            specs = settings.desired_plugin_specs
                            if specs:
                                ui.separator().classes('my-2 bg-zinc-800/40')
                                ui.label(t('core.settings.system.parsed_specs')).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold mb-1')
                                for spec in specs:
                                    with ui.row().classes('items-center gap-2 py-1'):
                                        ui.icon('subdirectory_arrow_right', size='14px').classes('text-zinc-600')
                                        ui.label(spec['url']).classes('text-xs font-mono text-zinc-300 flex-grow truncate')
                                        ui.label(f"@ {spec['version']}").classes('text-xs font-mono text-sky-400 shrink-0')

                    # API Integration
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-sky-400 via-cyan-400 to-indigo-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-3'):
                            with ui.row().classes('items-center gap-2 mb-1'):
                                ui.icon('api', size='18px').classes('text-sky-400')
                                ui.label(t('core.settings.system.api_integration')).classes(UIStyles.TITLE_H3)
                            ui.label(t('core.settings.system.github_rate_limit_hint')).classes(UIStyles.TEXT_MUTED + ' text-xs')

                            current_token = ''
                            if vault_instance.is_connected:
                                try:
                                    resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                                        path='core/settings', mount_point='lyndrix')
                                    current_token = resp['data']['data'].get('github_token', '')
                                except Exception:
                                    pass

                            gh_input = ui.input(t('core.settings.system.github_api_token'), value=current_token, password=True).classes('w-full max-w-md').props('outlined dark')

                            def save_github_token():
                                if not vault_instance.is_connected:
                                    ui.notify(t('core.settings.system.vault_not_connected'), type='warning')
                                    return
                                try:
                                    data = {}
                                    try:
                                        resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                                            path='core/settings', mount_point='lyndrix')
                                        data = resp['data']['data']
                                    except Exception:
                                        pass
                                    data['github_token'] = gh_input.value
                                    vault_instance.client.secrets.kv.v2.create_or_update_secret(
                                        path='core/settings', mount_point='lyndrix', secret=data)
                                    ui.notify(t('core.settings.system.token_saved'), type='positive')
                                except Exception as e:
                                    ui.notify(t('core.settings.system.error', error=str(e)), type='negative')

                            vault_ok = vault_instance.is_connected
                            vault_cls = 'text-emerald-400' if vault_ok else 'text-red-400'
                            with ui.row().classes('items-center gap-3 mt-1'):
                                ui.button(t('core.settings.system.save_token'), icon='save', on_click=save_github_token).props('outline size=sm color=primary')
                                with ui.row().classes(f'items-center gap-1 {vault_cls}'):
                                    ui.icon('lock' if vault_ok else 'lock_open', size='14px')
                                    ui.label(t('core.settings.system.vault_connected') if vault_ok else t('core.settings.system.vault_offline')).classes('text-xs')

                            # ── System API Key ───────────────────────────────
                            ui.separator().classes('my-3 bg-zinc-800/40')
                            with ui.row().classes('items-center gap-2 mb-1'):
                                ui.icon('vpn_key', size='18px').classes('text-amber-400')
                                ui.label('System API Key').classes(UIStyles.TITLE_H3)

                            env_key = os.getenv('LYNDRIX_SYSTEM_API_KEY')
                            env_locked = bool(env_key and env_key.strip())

                            if env_locked:
                                ui.label(
                                    'Set via the LYNDRIX_SYSTEM_API_KEY environment variable. '
                                    'The environment value takes precedence and cannot be overridden here.'
                                ).classes(UIStyles.TEXT_MUTED + ' text-xs')
                            else:
                                ui.label(
                                    'Master key for machine-to-machine API access (header X-API-Key or '
                                    'Authorization: Bearer). Unset by default — leave empty to keep the '
                                    'API-key method disabled.'
                                ).classes(UIStyles.TEXT_MUTED + ' text-xs')

                            current_api_key = ''
                            if vault_instance.is_connected:
                                try:
                                    resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                                        path='core/settings', mount_point='lyndrix')
                                    current_api_key = resp['data']['data'].get('system_api_key', '')
                                except Exception:
                                    pass

                            api_key_input = ui.input(
                                'System API Key',
                                value=current_api_key,
                                password=True,
                            ).classes('w-full max-w-md').props('outlined dark')
                            if env_locked:
                                api_key_input.props('readonly').classes('opacity-60')

                            def save_api_key():
                                if env_locked:
                                    ui.notify(
                                        'Key is locked by the LYNDRIX_SYSTEM_API_KEY environment variable.',
                                        type='warning',
                                    )
                                    return
                                if not vault_instance.is_connected:
                                    ui.notify(t('core.settings.system.vault_not_connected'), type='warning')
                                    return
                                try:
                                    data = {}
                                    try:
                                        resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                                            path='core/settings', mount_point='lyndrix')
                                        data = resp['data']['data']
                                    except Exception:
                                        pass
                                    new_value = (api_key_input.value or '').strip()
                                    if new_value:
                                        data['system_api_key'] = new_value
                                    else:
                                        data.pop('system_api_key', None)
                                    vault_instance.client.secrets.kv.v2.create_or_update_secret(
                                        path='core/settings', mount_point='lyndrix', secret=data)
                                    ui.notify(
                                        'System API key saved.' if new_value else 'System API key cleared (API-key method disabled).',
                                        type='positive',
                                    )
                                except Exception as e:
                                    ui.notify(t('core.settings.system.error', error=str(e)), type='negative')

                            with ui.row().classes('items-center gap-3 mt-1'):
                                save_btn = ui.button('Save API Key', icon='save', on_click=save_api_key).props('outline size=sm color=primary')
                                if env_locked:
                                    save_btn.props('disable')
                                    with ui.row().classes('items-center gap-1 text-amber-400'):
                                        ui.icon('lock', size='14px')
                                        ui.label('Locked by environment').classes('text-xs')

            # ── AUTH ─────────────────────────────────────────────────────────
            with ui.tab_panel(tab_auth).classes('p-0'):
                await render_auth_settings_card()

            # ── GRUPPEN ──────────────────────────────────────────────────────
            with ui.tab_panel(tab_groups).classes('p-0'):
                await render_groups_card()

            # ── BERECHTIGUNGEN ────────────────────────────────────────────────
            with ui.tab_panel(tab_perms).classes('p-0'):
                await render_permissions_card()

            # ── PLUGINS ─────────────────────────────────────────────────────
            with ui.tab_panel(tab_plugins).classes('p-0'):
                render_plugin_manager()

            # ── INFO ────────────────────────────────────────────────────────
            with ui.tab_panel(tab_info).classes('p-0'):
                with ui.column().classes('w-full gap-4'):

                    # Version badge
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-indigo-500 via-violet-500 to-purple-500')
                        with ui.row().classes('w-full flex-grow items-center gap-5 p-5'):
                            ui.icon('rocket_launch', size='40px').classes('text-indigo-400 shrink-0')
                            with ui.column().classes('gap-1 flex-grow'):
                                with ui.row().classes('items-baseline gap-3'):
                                    ui.label('Lyndrix Core').classes('text-xl font-black tracking-tight')
                                    ui.label(f'v{__version__}').classes('text-base font-mono font-bold text-indigo-400')
                                with ui.row().classes('items-center gap-2'):
                                    ui.label(__codename__).classes(
                                        'text-xs font-bold px-3 py-0.5 rounded-full '
                                        'bg-violet-500/15 text-violet-300 border border-violet-500/30')
                                    ui.label(t('core.settings.info.released', date=__release_date__)).classes('text-xs text-zinc-500')

                    # Runtime
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-sky-400 to-cyan-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('terminal', size='18px').classes('text-sky-400')
                                ui.label(t('core.settings.info.runtime_environment')).classes(UIStyles.TITLE_H3)
                            _kv_row(t('core.settings.info.python'), f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}', mono=True)
                            _kv_row(t('core.settings.info.platform'), platform.platform(), mono=True)
                            _kv_row(t('core.settings.info.architecture'), platform.machine())
                            _kv_row(t('core.settings.info.node_hostname'), platform.node(), mono=True)
                            with ui.row().classes('w-full items-center justify-between py-2'):
                                ui.label(t('core.settings.info.uptime')).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold shrink-0')
                                uptime_label = ui.label(get_uptime()).classes('text-sm font-mono text-zinc-100')
                            ui.timer(5.0, lambda: uptime_label.set_text(get_uptime()))

                    # System Resources
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 to-teal-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('memory', size='18px').classes('text-emerald-400')
                                ui.label(t('core.settings.info.system_resources')).classes(UIStyles.TITLE_H3)
                            with ui.row().classes('w-full items-center justify-between py-2 border-b border-zinc-800/40'):
                                ui.label(t('core.settings.info.cpu')).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold')
                                cpu_label = ui.label('–').classes('text-sm font-mono text-zinc-100')
                            with ui.row().classes('w-full items-center justify-between py-2 border-b border-zinc-800/40'):
                                ui.label(t('core.settings.info.memory')).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold')
                                mem_label = ui.label('–').classes('text-sm font-mono text-zinc-100')
                            with ui.row().classes('w-full items-center justify-between py-2'):
                                ui.label(t('core.settings.info.disk_root')).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold')
                                disk_label = ui.label('–').classes('text-sm font-mono text-zinc-100')

                        def update_resources():
                            cpu_pct = psutil.cpu_percent(interval=None)
                            mem    = psutil.virtual_memory()
                            disk   = psutil.disk_usage('/')
                            cpu_label.set_text(f'{psutil.cpu_count()} vCPUs  ·  {cpu_pct:.1f}% load')
                            mem_label.set_text(f'{mem.used / 1e9:.1f} / {mem.total / 1e9:.1f} GB  ({mem.percent:.0f}%)')
                            disk_label.set_text(f'{disk.used / 1e9:.1f} / {disk.total / 1e9:.1f} GB  ({disk.percent:.0f}%)')

                        update_resources()
                        ui.timer(10.0, update_resources)

                    # Module Registry
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-amber-400 to-orange-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('widgets', size='18px').classes('text-amber-400')
                                ui.label(t('core.settings.info.module_registry')).classes(UIStyles.TITLE_H3)
                            all_mods = list(module_manager.registry.items())
                            _kv_row(t('core.settings.info.total_registered'), str(len(all_mods)))
                            _kv_row(t('core.common.active'), str(sum(1 for _, e in all_mods if e.get('status') == 'active')), value_cls='text-emerald-400 font-bold')
                            _kv_row(t('core.settings.info.core'), str(sum(1 for _, e in all_mods if e['manifest'].type == 'CORE')))
                            _kv_row(t('core.settings.info.plugins'), str(sum(1 for _, e in all_mods if e['manifest'].type == 'PLUGIN')))

                    # Service Health
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-rose-400 to-pink-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('hub', size='18px').classes('text-rose-400')
                                ui.label(t('core.settings.info.service_health')).classes(UIStyles.TITLE_H3)
                            for svc_name, connected, detail in [
                                (t('core.settings.info.database'), db_instance.is_connected, settings.DATABASE_URL_SAFE),
                                (t('core.settings.info.vault'), vault_instance.is_connected, settings.VAULT_URL),
                            ]:
                                s_cls = 'text-emerald-400' if connected else 'text-red-400'
                                with ui.row().classes('w-full items-center justify-between py-2 border-b border-zinc-800/40 last:border-0'):
                                    with ui.row().classes('items-center gap-2'):
                                        ui.icon('circle', size='10px').classes(s_cls)
                                        ui.label(svc_name).classes('text-xs uppercase tracking-widest text-zinc-500 font-bold')
                                    with ui.row().classes('items-center gap-3'):
                                        ui.label(detail).classes('text-xs font-mono text-zinc-600 truncate max-w-xs')
                                        ui.label(t('core.settings.info.online') if connected else t('core.settings.info.offline')).classes(f'text-sm font-bold {s_cls}')


