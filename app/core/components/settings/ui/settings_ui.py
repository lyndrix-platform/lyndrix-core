import io
import os
import platform
import sys
import zipfile
from pathlib import Path

import psutil
from nicegui import ui

from config import settings
from core.i18n import t
from core.logger import get_logger
from core.theming import get_theme_engine
from core.theming.engine import THEMES_BASE_DIR
from ui.theme import UIStyles
from version import __version__, __codename__, __release_date__, get_uptime

from core.components.auth.ui.auth_cards import render_user_settings_card
from core.components.auth.ui.auth_settings_card import render_auth_settings_card
from core.components.auth.ui.groups_card import render_groups_card
from core.components.auth.ui.permissions_card import render_permissions_card
from core.components.plugins.ui.plugins_ui import render_plugin_manager
from core.components.plugins.logic.manager import module_manager
from core.components.notification_router.ui.notifications_settings import (
    render_notifications_settings_card,
)
from core.services import vault_instance, db_instance

log = get_logger("UI:Settings")


def _kv_row(label: str, value: str, mono: bool = False, value_cls: str = ''):
    """Renders a labeled key/value row inside a column container."""
    with ui.row().classes('w-full items-center justify-between py-2 border-b border-slate-200 dark:border-white/10 last:border-0'):
        ui.label(label).classes(UIStyles.LABEL_MINI + ' shrink-0')
        cls = 'text-sm font-mono' if mono else 'text-sm'
        cls += f' {value_cls}' if value_cls else ' text-slate-900 dark:text-zinc-100'
        ui.label(value).classes(cls)


def _render_editable_settings_card() -> None:
    """
    Render the editable application settings.

    Values are persisted to Vault (lyndrix/core/settings) and applied to the live
    Settings object immediately.  Fields whose OS environment variable is set are
    shown read-only because the env var overrides them on the next boot.
    """
    from config import EDITABLE_SETTINGS, EDITABLE_SETTING_CATEGORIES, settings

    widgets: dict = {}
    locked: dict = {}

    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-indigo-400 via-sky-400 to-cyan-400')
        with ui.column().classes('w-full flex-grow p-5 gap-2'):
            with ui.row().classes('items-center gap-2 mb-1'):
                ui.icon('tune', size='18px').classes('text-indigo-400')
                ui.label('Application Settings').classes(UIStyles.TITLE_H3)

            for category in EDITABLE_SETTING_CATEGORIES:
                specs = [s for s in EDITABLE_SETTINGS if s.category == category]
                if not specs:
                    continue
                ui.label(category).classes(
                    UIStyles.LABEL_MINI + ' mt-3'
                )
                for spec in specs:
                    is_locked = os.getenv(spec.field) is not None
                    locked[spec.field] = is_locked
                    current = getattr(settings, spec.field, '')

                    with ui.column().classes('w-full gap-0 py-2 border-b border-slate-200 dark:border-white/10 last:border-0'):
                        with ui.row().classes('w-full items-center justify-between gap-4'):
                            with ui.column().classes('gap-0 flex-grow min-w-0'):
                                ui.label(spec.label).classes('text-sm font-bold text-slate-800 dark:text-zinc-200')
                                ui.label(f'{spec.description}  ·  Env: {spec.env_var}').classes(
                                    UIStyles.TEXT_HINT
                                )

                            if spec.kind == 'bool':
                                widget = ui.switch(value=bool(current))
                            elif spec.kind == 'select':
                                widget = ui.select(
                                    options=spec.options, value=str(current),
                                ).props('dense options-dense outlined').classes('w-44')
                            elif spec.kind == 'int':
                                widget = ui.number(value=current).props('dense outlined').classes('w-44')
                            else:
                                widget = ui.input(
                                    value='' if current is None else str(current),
                                    password=spec.sensitive,
                                ).props('outlined dark dense').classes('w-72 max-w-full')

                            if is_locked:
                                widget.props('readonly disable').classes('opacity-60')
                            widgets[spec.field] = widget

                        if is_locked:
                            with ui.row().classes('items-center gap-1 text-amber-400 mt-1'):
                                ui.icon('lock', size='12px')
                                ui.label('Locked by environment variable').classes('text-[11px]')

            def _save():
                if not vault_instance.is_connected:
                    ui.notify(t('core.settings.system.vault_not_connected'), type='warning')
                    return
                data = {}
                try:
                    resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                        path='core/settings', mount_point='lyndrix')
                    data = resp['data']['data'] or {}
                except Exception:
                    data = {}

                applied = []
                for spec in EDITABLE_SETTINGS:
                    if locked.get(spec.field):
                        continue  # env-locked — never persist/override
                    widget = widgets.get(spec.field)
                    if widget is None:
                        continue
                    try:
                        value = spec.coerce(widget.value)
                    except Exception:
                        ui.notify(f'Invalid value for {spec.label}.', type='negative')
                        return
                    data[spec.field] = value
                    setattr(settings, spec.field, value)
                    applied.append(spec.field)
                    if spec.field == 'LOG_LEVEL':
                        import logging
                        logging.getLogger().setLevel(getattr(logging, str(value), logging.INFO))

                try:
                    vault_instance.client.secrets.kv.v2.create_or_update_secret(
                        path='core/settings', mount_point='lyndrix', secret=data)
                except Exception as e:
                    ui.notify(t('core.settings.system.error', error=str(e)), type='negative')
                    return
                ui.notify(
                    f'Saved {len(applied)} setting(s) — applied now and persisted for next boot.',
                    type='positive',
                )

            with ui.row().classes('w-full justify-end mt-3'):
                ui.button('Save settings', icon='save', on_click=_save).props(
                    'unelevated size=sm color=primary')

def _render_theme_management_card() -> None:
    """Theme selector, palette preview, upload and delete."""
    engine = get_theme_engine()
    available = engine.list_available_themes()
    active_id = settings.DEFAULT_THEME_ID

    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
        ui.element('div').classes(UIStyles.GRAD_BAR_ACCENT)
        with ui.column().classes('w-full flex-grow p-5 gap-4'):
            with ui.row().classes('items-center gap-2 mb-1'):
                ui.icon('palette', size='18px').classes(UIStyles.ICON_INFO)
                ui.label('Active Theme').classes(UIStyles.TITLE_H3)

            # ── Palette preview ──────────────────────────────────────────────
            try:
                theme = engine.resolve_theme(active_id)
                palette = engine.resolve_runtime_palette(dark=True, theme_id=active_id)
                with ui.row().classes('items-center gap-2 flex-wrap'):
                    ui.label(active_id).classes(
                        'text-xs font-bold uppercase tracking-wider px-3 py-1 rounded-full '
                        'bg-cyan-500/15 text-cyan-300 border border-cyan-500/30'
                    )
                    for name, color in palette.items():
                        with ui.element('div').classes('flex flex-col items-center gap-1'):
                            ui.element('div').style(
                                f'width:20px;height:20px;border-radius:50%;background:{color};'
                                f'border:1px solid rgba(255,255,255,0.15)'
                            ).tooltip(f'{name}: {color}')
            except Exception:
                ui.label('Could not load theme preview.').classes(UIStyles.TEXT_MUTED)

            ui.separator().classes('bg-slate-200 dark:bg-white/10')

            # ── Theme switcher ───────────────────────────────────────────────
            with ui.row().classes('items-center gap-3 w-full flex-wrap'):
                ui.label('Switch Theme').classes('text-sm font-bold text-slate-800 dark:text-zinc-200 shrink-0')
                theme_select = ui.select(
                    options=available,
                    value=active_id,
                ).props('dense outlined options-dense').classes('w-48')

                def _apply_theme():
                    new_id = theme_select.value
                    if not new_id or new_id == settings.DEFAULT_THEME_ID:
                        return
                    settings.DEFAULT_THEME_ID = new_id
                    if vault_instance.is_connected:
                        try:
                            data: dict = {}
                            try:
                                resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                                    path='core/settings', mount_point='lyndrix')
                                data = resp['data']['data'] or {}
                            except Exception:
                                pass
                            data['DEFAULT_THEME_ID'] = new_id
                            vault_instance.client.secrets.kv.v2.create_or_update_secret(
                                path='core/settings', mount_point='lyndrix', secret=data)
                        except Exception as e:
                            ui.notify(f'Could not persist to Vault: {e}', type='warning')
                    ui.notify(
                        f'Theme switched to "{new_id}". Reload the page to apply.',
                        type='positive',
                    )

                ui.button('Apply', icon='check', on_click=_apply_theme).props(
                    'unelevated size=sm color=primary'
                )

            ui.separator().classes('bg-slate-200 dark:bg-white/10')

            # ── Upload new theme ─────────────────────────────────────────────
            with ui.column().classes('w-full gap-2'):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('upload_file', size='18px').classes(UIStyles.ICON_MUTED)
                    ui.label('Upload Theme').classes('text-sm font-bold text-slate-800 dark:text-zinc-200')
                ui.label(
                    'Upload a ZIP containing tokens.json and components.json. '
                    'The ZIP filename (without .zip) becomes the theme ID.'
                ).classes(UIStyles.TEXT_HINT)

                def _handle_upload(e):
                    try:
                        data = e.content.read()
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            names = zf.namelist()
                            if 'tokens.json' not in names or 'components.json' not in names:
                                ui.notify(
                                    'ZIP must contain tokens.json and components.json at root level.',
                                    type='negative',
                                )
                                return
                            theme_id = Path(e.name).stem
                            if not theme_id or '/' in theme_id or '.' in theme_id:
                                ui.notify('Invalid theme ID derived from filename.', type='negative')
                                return
                            dest = THEMES_BASE_DIR / theme_id
                            dest.mkdir(parents=True, exist_ok=True)
                            zf.extract('tokens.json', dest)
                            zf.extract('components.json', dest)

                        from core.theming.schema import validate_theme_pack
                        from core.theming.loader import load_theme_pack
                        try:
                            pack = load_theme_pack(THEMES_BASE_DIR, theme_id)
                            errors = validate_theme_pack(pack)
                        except Exception as ve:
                            ui.notify(f'Theme validation failed: {ve}', type='negative')
                            return

                        if errors:
                            ui.notify(
                                f'Theme "{theme_id}" uploaded with {len(errors)} missing key(s) '
                                f'(backfilled with defaults). Reload to see it in the list.',
                                type='warning',
                            )
                        else:
                            ui.notify(
                                f'Theme "{theme_id}" uploaded and validated. '
                                f'Reload the page to select it.',
                                type='positive',
                            )
                    except zipfile.BadZipFile:
                        ui.notify('Invalid ZIP file.', type='negative')
                    except Exception as ex:
                        ui.notify(f'Upload failed: {ex}', type='negative')

                ui.upload(
                    label='Drop theme ZIP here',
                    on_upload=_handle_upload,
                    auto_upload=True,
                ).props('accept=".zip" flat bordered').classes('w-full max-w-md')

            ui.separator().classes('bg-slate-200 dark:bg-white/10')

            # ── Delete theme ─────────────────────────────────────────────────
            deletable = [t for t in available if t != 'default']
            if deletable:
                with ui.row().classes('items-center gap-3 flex-wrap'):
                    ui.label('Delete Theme').classes('text-sm font-bold text-slate-800 dark:text-zinc-200 shrink-0')
                    del_select = ui.select(
                        options=deletable,
                        value=deletable[0] if deletable else None,
                    ).props('dense outlined options-dense').classes('w-48')

                    def _delete_theme():
                        tid = del_select.value
                        if not tid or tid == 'default':
                            return
                        import shutil
                        target = THEMES_BASE_DIR / tid
                        if target.exists():
                            shutil.rmtree(target)
                        if settings.DEFAULT_THEME_ID == tid:
                            settings.DEFAULT_THEME_ID = 'default'
                        ui.notify(f'Theme "{tid}" deleted. Reload to apply.', type='positive')

                    ui.button('Delete', icon='delete', on_click=_delete_theme).props(
                        'outline size=sm color=negative'
                    )
            else:
                ui.label('No custom themes installed yet.').classes(UIStyles.TEXT_HINT)


async def render_settings_page():
    """Renders the complete Settings dashboard."""

    with ui.column().classes('w-full max-w-5xl mx-auto gap-6'):
        # ── Page Header ─────────────────────────────────────────────────────
        with ui.row().classes('w-full items-center gap-4'):
            ui.element('div').classes('h-12 w-1 rounded-full bg-gradient-to-b from-indigo-400 to-sky-400')
            with ui.column().classes('gap-0'):
                ui.label(t('core.settings.page.title')).classes(UIStyles.TITLE_H2)
                ui.label(t('core.settings.page.subtitle')).classes(UIStyles.TEXT_MUTED + ' uppercase tracking-widest text-xs')

        ui.separator().classes('bg-slate-200 dark:bg-white/10')

        # ── Tabs ────────────────────────────────────────────────────────────
        with ui.tabs().classes(UIStyles.TAB_BAR) as tabs:
            tab_profile       = ui.tab(t('core.settings.tabs.profile'),       icon='person')
            tab_system        = ui.tab(t('core.settings.tabs.system'),        icon='tune')
            tab_notifications = ui.tab(t('core.settings.tabs.notifications'), icon='campaign')
            tab_appearance    = ui.tab('Appearance',                           icon='palette')
            tab_auth          = ui.tab(t('core.settings.tabs.auth'),          icon='security')
            tab_groups        = ui.tab(t('core.settings.tabs.groups'),        icon='group')
            tab_perms         = ui.tab(t('core.settings.tabs.permissions'),   icon='verified_user')
            tab_plugins       = ui.tab(t('core.settings.tabs.plugins'),       icon='extension')
            tab_info          = ui.tab(t('core.settings.tabs.info'),          icon='info')

        with ui.tab_panels(tabs, value=tab_profile).classes('w-full bg-transparent p-0 mt-4'):

            # ── PROFIL ──────────────────────────────────────────────────────
            with ui.tab_panel(tab_profile).classes('p-0'):
                render_user_settings_card()

            # ── SYSTEM ──────────────────────────────────────────────────────
            with ui.tab_panel(tab_system).classes('p-0'):
                with ui.column().classes('w-full gap-4'):

                    # Disclaimer — env vars override UI-saved settings on boot
                    with ui.row().classes(
                        'w-full items-start gap-3 p-4 '
                        'bg-amber-500/10 border border-amber-500/30 rounded-xl'
                    ):
                        ui.icon('warning_amber', size='20px').classes('text-amber-400 shrink-0 mt-0.5')
                        with ui.column().classes('gap-0'):
                            ui.label('Environment variables take precedence').classes(
                                'text-sm font-bold text-amber-300'
                            )
                            ui.label(
                                'Settings saved here are stored in Vault and re-applied on the next boot. '
                                'However, any setting that is also provided via its environment variable will '
                                'be overridden by that environment variable on the next boot — the env var '
                                'always wins. Fields locked by an environment variable are read-only below.'
                            ).classes('text-xs text-amber-200/80')

                    # Editable application settings
                    _render_editable_settings_card()

                    # Read-only system info (not runtime-editable)
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-zinc-500 via-zinc-400 to-zinc-500')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('dns', size='18px').classes('text-zinc-400')
                                ui.label('System Info').classes(UIStyles.TITLE_H3)
                            env_cls = 'text-amber-400 font-bold' if settings.ENV_TYPE == 'dev' else 'text-emerald-400 font-bold'
                            _kv_row('Environment  ·  Env: ENV_TYPE', settings.ENV_TYPE.upper(), value_cls=env_cls)
                            _kv_row('Port  ·  Env: PORT', str(settings.PORT), mono=True)

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
                                ui.separator().classes('my-2 bg-slate-200 dark:bg-white/10')
                                ui.label(t('core.settings.system.parsed_specs')).classes(UIStyles.LABEL_MINI + ' mb-1')
                                for spec in specs:
                                    with ui.row().classes('items-center gap-2 py-1'):
                                        ui.icon('subdirectory_arrow_right', size='14px').classes('text-zinc-600')
                                        ui.label(spec['url']).classes(UIStyles.TEXT_HINT + ' font-mono flex-grow truncate')
                                        ui.label(f"@ {spec['version']}").classes('text-xs font-mono text-sky-400 shrink-0')

                    # API Integration
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-sky-400 via-cyan-400 to-indigo-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-3'):
                            with ui.row().classes('items-center gap-2 mb-1'):
                                ui.icon('api', size='18px').classes('text-sky-400')
                                ui.label(t('core.settings.system.api_integration')).classes(UIStyles.TITLE_H3)
                            ui.label(t('core.settings.system.github_rate_limit_hint')).classes(UIStyles.TEXT_HINT)
                            ui.label('GitHub Personal Access Token, stored in Vault.  ·  Env: GITHUB_TOKEN (overrides the stored value when set)').classes(UIStyles.TEXT_HINT)

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

                            # ── GitLab token (for custom plugin repositories) ──
                            ui.separator().classes('my-3 bg-slate-200 dark:bg-white/10')
                            ui.label('GitLab Personal Access Token, stored in Vault.  ·  Env: GITLAB_TOKEN (overrides the stored value when set)').classes(UIStyles.TEXT_HINT)

                            current_gl_token = ''
                            if vault_instance.is_connected:
                                try:
                                    resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                                        path='core/settings', mount_point='lyndrix')
                                    current_gl_token = resp['data']['data'].get('gitlab_token', '')
                                except Exception:
                                    pass

                            gl_input = ui.input(t('core.settings.system.gitlab_api_token'), value=current_gl_token, password=True).classes('w-full max-w-md').props('outlined dark')

                            def save_gitlab_token():
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
                                    data['gitlab_token'] = gl_input.value
                                    vault_instance.client.secrets.kv.v2.create_or_update_secret(
                                        path='core/settings', mount_point='lyndrix', secret=data)
                                    ui.notify(t('core.settings.system.token_saved'), type='positive')
                                except Exception as e:
                                    ui.notify(t('core.settings.system.error', error=str(e)), type='negative')

                            with ui.row().classes('items-center gap-3 mt-1'):
                                ui.button(t('core.settings.system.save_token'), icon='save', on_click=save_gitlab_token).props('outline size=sm color=primary')

                            # ── System API Key ───────────────────────────────
                            ui.separator().classes('my-3 bg-slate-200 dark:bg-white/10')
                            with ui.row().classes('items-center gap-2 mb-1'):
                                ui.icon('vpn_key', size='18px').classes('text-amber-400')
                                ui.label('System API Key').classes(UIStyles.TITLE_H3)
                            ui.label('Env: LYNDRIX_SYSTEM_API_KEY').classes(UIStyles.TEXT_HINT)

                            env_key = os.getenv('LYNDRIX_SYSTEM_API_KEY')
                            env_locked = bool(env_key and env_key.strip())

                            if env_locked:
                                ui.label(
                                    'Set via the LYNDRIX_SYSTEM_API_KEY environment variable. '
                                    'The environment value takes precedence and cannot be overridden here.'
                                ).classes(UIStyles.TEXT_HINT)
                            else:
                                ui.label(
                                    'Master key for machine-to-machine API access (header X-API-Key or '
                                    'Authorization: Bearer). Unset by default — leave empty to keep the '
                                    'API-key method disabled.'
                                ).classes(UIStyles.TEXT_HINT)

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

            # ── NOTIFICATIONS ───────────────────────────────────────────────
            with ui.tab_panel(tab_notifications).classes('p-0'):
                render_notifications_settings_card()

            # ── APPEARANCE ───────────────────────────────────────────────────
            with ui.tab_panel(tab_appearance).classes('p-0'):
                with ui.column().classes('w-full gap-4'):
                    _render_theme_management_card()

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
                                    ui.label(t('core.settings.info.released', date=__release_date__)).classes(UIStyles.TEXT_HINT)

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
                                ui.label(t('core.settings.info.uptime')).classes(UIStyles.LABEL_MINI + ' shrink-0')
                                uptime_label = ui.label(get_uptime()).classes('text-sm font-mono text-slate-900 dark:text-zinc-100')
                            ui.timer(5.0, lambda: uptime_label.set_text(get_uptime()))

                    # System Resources
                    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 to-teal-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-1'):
                            with ui.row().classes('items-center gap-2 mb-3'):
                                ui.icon('memory', size='18px').classes('text-emerald-400')
                                ui.label(t('core.settings.info.system_resources')).classes(UIStyles.TITLE_H3)
                            with ui.row().classes('w-full items-center justify-between py-2 border-b border-slate-200 dark:border-white/10'):
                                ui.label(t('core.settings.info.cpu')).classes(UIStyles.LABEL_MINI)
                                cpu_label = ui.label('–').classes('text-sm font-mono text-slate-900 dark:text-zinc-100')
                            with ui.row().classes('w-full items-center justify-between py-2 border-b border-slate-200 dark:border-white/10'):
                                ui.label(t('core.settings.info.memory')).classes(UIStyles.LABEL_MINI)
                                mem_label = ui.label('–').classes('text-sm font-mono text-slate-900 dark:text-zinc-100')
                            with ui.row().classes('w-full items-center justify-between py-2'):
                                ui.label(t('core.settings.info.disk_root')).classes(UIStyles.LABEL_MINI)
                                disk_label = ui.label('–').classes('text-sm font-mono text-slate-900 dark:text-zinc-100')

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
                                with ui.row().classes('w-full items-center justify-between py-2 border-b border-slate-200 dark:border-white/10 last:border-0'):
                                    with ui.row().classes('items-center gap-2'):
                                        ui.icon('circle', size='10px').classes(s_cls)
                                        ui.label(svc_name).classes(UIStyles.LABEL_MINI)
                                    with ui.row().classes('items-center gap-3'):
                                        ui.label(detail).classes(UIStyles.TEXT_HINT + ' font-mono truncate max-w-xs')
                                        ui.label(t('core.settings.info.online') if connected else t('core.settings.info.offline')).classes(f'text-sm font-bold {s_cls}')


