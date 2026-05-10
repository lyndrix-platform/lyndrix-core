from nicegui import ui, app

from ui.theme import UIStyles
from core.logger import get_logger
from core.i18n import t

log = get_logger("UI:AuthCards")


def render_user_settings_card():
    from core.components.auth.logic.user_service import user_service
    from core.components.auth.logic.group_service import group_service

    session = app.storage.user
    username     = str(session.get('username', ''))
    full_name    = str(session.get('full_name', 'Benutzer'))
    email        = str(session.get('email', '—'))
    provider     = str(session.get('auth_provider', 'local'))
    roles        = list(session.get('roles', []))
    extra_perms  = list(session.get('extra_permissions', []))

    initials = ''.join(p[0].upper() for p in full_name.split()[:2]) or 'U'
    is_local = (provider == 'local')

    with ui.column().classes('w-full gap-4'):

        # ── Profile banner ───────────────────────────────────────────────────
        with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
            ui.element('div').classes(
                'h-1 w-full bg-gradient-to-r from-indigo-400 via-violet-400 to-purple-400'
            )
            with ui.row().classes('w-full flex-grow items-center gap-5 p-5'):
                ui.avatar(initials, color='indigo', text_color='white').classes(
                    'text-2xl font-black shrink-0'
                )
                with ui.column().classes('gap-1 flex-grow'):
                    ui.label(full_name).classes('text-xl font-bold text-zinc-100')
                    with ui.row().classes('items-center gap-2'):
                        ui.label(f'@{username}').classes('text-sm font-mono text-zinc-400')
                        if email and email != '—':
                            ui.label('·').classes('text-zinc-600')
                            ui.label(email).classes('text-sm text-zinc-500')

                with ui.column().classes('items-end gap-1 shrink-0'):
                    # Provider badge
                    badge_cls = (
                        'bg-indigo-500/15 text-indigo-300 border border-indigo-500/30'
                        if is_local else
                        'bg-violet-500/15 text-violet-300 border border-violet-500/30'
                    )
                    ui.label(provider).classes(
                        f'text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full {badge_cls}'
                    )
                    # Roles / groups chips
                    if roles:
                        with ui.row().classes('flex-wrap gap-1 justify-end'):
                            for role in roles[:6]:  # cap display at 6
                                ui.label(role).classes(
                                    'text-[9px] font-mono px-2 py-0.5 rounded-full '
                                    'bg-zinc-700/60 text-zinc-400 border border-zinc-700/40'
                                )
                            if len(roles) > 6:
                                ui.label(f'+{len(roles) - 6}').classes(
                                    'text-[9px] text-zinc-600'
                                )

        # ── Password change ──────────────────────────────────────────────────
        if is_local:
            _render_password_change_card(username)
        else:
            with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-zinc-700')
                with ui.row().classes('w-full items-center gap-3 p-5'):
                    ui.icon('info', size='18px').classes('text-zinc-500 shrink-0')
                    ui.label(
                        t('Passwortänderung wird von deinem Auth-Provider "%{provider}" verwaltet. '
                          'Ändere dein Passwort dort.', provider=provider)
                    ).classes('text-sm text-zinc-500')

        # ── Direct permissions ───────────────────────────────────────────────
        if extra_perms:
            with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-teal-400 to-emerald-400')
                with ui.column().classes('w-full flex-grow p-5 gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('verified_user', size='18px').classes('text-teal-400')
                        ui.label(t('Direkt zugewiesene Berechtigungen')).classes(UIStyles.TITLE_H3)
                    with ui.row().classes('flex-wrap gap-1 mt-1'):
                        for perm in extra_perms:
                            ui.label(perm).classes(
                                'text-[10px] font-mono px-2 py-0.5 rounded-full '
                                'bg-teal-500/15 text-teal-300 border border-teal-500/30'
                            )

        # ── Logout ───────────────────────────────────────────────────────────
        with ui.row().classes('w-full justify-end'):
            async def logout():
                app.storage.user.clear()
                ui.navigate.to('/login')
            ui.button(t('Abmelden'), icon='logout', on_click=logout).props(
                'outline color=negative size=sm'
            )


def _render_password_change_card(username: str) -> None:
    from core.components.auth.logic.user_service import user_service

    with ui.card().classes('w-full bg-zinc-900/60 border border-zinc-800 rounded-2xl').style(
        'padding: 0; flex-wrap: nowrap'
    ):
        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-sky-400 to-indigo-400')
        with ui.column().classes('w-full flex-grow p-5 gap-3'):
            with ui.row().classes('items-center gap-2 mb-1'):
                ui.icon('lock_reset', size='18px').classes('text-sky-400')
                ui.label(t('Passwort ändern')).classes('text-base font-bold text-zinc-100')

            warn = user_service.env_override_warning(username)
            if warn:
                with ui.row().classes(
                    'w-full items-start gap-2 p-3 mb-1 '
                    'bg-amber-500/10 border border-amber-500/30 rounded-xl'
                ):
                    ui.icon('warning_amber', size='16px').classes('text-amber-400 shrink-0 mt-0.5')
                    ui.label(warn).classes('text-xs text-amber-400/80')

            cur_pw  = ui.input(
                t('Aktuelles Passwort'), password=True, password_toggle_button=True
            ).props('outlined dark dense autocomplete=current-password').classes('w-full max-w-sm')
            new_pw  = ui.input(
                t('Neues Passwort'), password=True, password_toggle_button=True
            ).props('outlined dark dense autocomplete=new-password').classes('w-full max-w-sm')
            conf_pw = ui.input(
                t('Neues Passwort bestätigen'), password=True, password_toggle_button=True
            ).props('outlined dark dense autocomplete=new-password').classes('w-full max-w-sm')

            status_label = ui.label('').classes('text-xs')

            def do_change():
                if new_pw.value != conf_pw.value:
                    status_label.classes(
                        add='text-red-400', remove='text-emerald-400 text-zinc-500'
                    )
                    status_label.set_text(t('Passwörter stimmen nicht überein.'))
                    return
                ok, msg = user_service.change_password(
                    username, cur_pw.value, new_pw.value
                )
                if ok:
                    status_label.classes(
                        add='text-emerald-400', remove='text-red-400 text-zinc-500'
                    )
                    cur_pw.set_value('')
                    new_pw.set_value('')
                    conf_pw.set_value('')
                else:
                    status_label.classes(
                        add='text-red-400', remove='text-emerald-400 text-zinc-500'
                    )
                status_label.set_text(msg)

            ui.button(t('Passwort ändern'), icon='save', on_click=do_change).props(
                'unelevated size=sm color=primary'
            )
