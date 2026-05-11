"""
Groups management card for the Auth settings tab.

Allows admins to:
  - Create / edit / delete local groups
  - Define permissions per group (free-text strings, e.g. "dashboard.view")
  - Map LDAP group DNs to local groups so LDAP users inherit permissions on login
"""
from nicegui import ui

from core.logger import get_logger
from core.i18n import t
from ui.theme import UIStyles

log = get_logger("UI:GroupSettings")

# Well-known permission suggestions shown as chips for quick selection.
_SUGGESTED_PERMISSIONS = [
    "dashboard.view",
    "settings.view",
    "settings.edit",
    "api.read",
    "api.write",
    "plugins.manage",
    "users.view",
    "users.edit",
]


async def render_groups_card() -> None:
    """Render the full groups management UI."""
    from core.components.auth.logic.group_service import group_service
    from core.components.auth.logic.models import Group
    selected_id: dict = {"value": None}   # mutable container for closure capture

    # ── Outer layout: list on left, editor on right ──────────────────────────
    with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
        ui.element('div').classes(
            'h-1 w-full bg-gradient-to-r from-teal-400 via-emerald-400 to-green-400'
        )
        with ui.column().classes('w-full flex-grow p-5 gap-3'):
            with ui.row().classes('items-center gap-2 mb-1'):
                ui.icon('group', size='18px').classes('text-teal-400')
                ui.label(t('Lokale Gruppen & Berechtigungen')).classes(UIStyles.TITLE_H3)
            ui.label(
                t('Gruppen bündeln Berechtigungen. LDAP-Benutzer erhalten die Rechte einer Gruppe '
                  'automatisch, wenn ihr LDAP-Gruppen-DN gemappt ist.')
            ).classes(UIStyles.TEXT_MUTED + ' text-xs mb-2')

            with ui.row().classes('w-full gap-4 items-start'):

                # ── Left: group list ──────────────────────────────────────────
                group_list_col = ui.column().classes('gap-1 min-w-[200px] w-[200px] shrink-0')

                # ── Right: editor ─────────────────────────────────────────────
                editor_col = ui.column().classes('flex-grow gap-3')

            # ── New group row ─────────────────────────────────────────────────
            with ui.row().classes('w-full items-center gap-2 mt-2 pt-3 border-t border-zinc-800/40'):
                new_name_input = ui.input(
                    placeholder=t('Neuer Gruppenname…')
                ).props('outlined dark dense').classes('flex-grow max-w-xs')

                def create_group():
                    name = new_name_input.value.strip()
                    if not name:
                        ui.notify(t('Name darf nicht leer sein.'), type='warning')
                        return
                    if group_service.get_by_name(name):
                        ui.notify(t('Gruppe "%{name}" existiert bereits.', name=name), type='warning')
                        return
                    group_service.create(name)
                    new_name_input.set_value('')
                    _refresh_list()
                    ui.notify(t('Gruppe "%{name}" erstellt.', name=name), type='positive')

                ui.button('Erstellen', icon='add', on_click=create_group).props(
                    'unelevated size=sm color=teal'
                )

        # ── Refresh helpers ───────────────────────────────────────────────────

        def _open_editor(grp: Group) -> None:
            selected_id["value"] = grp.id
            editor_col.clear()
            with editor_col:
                _render_editor(grp)
            _refresh_list()   # re-render list to highlight selection

        def _render_editor(grp: Group) -> None:
            name_input = ui.input(
                t('Name'), value=grp.name
            ).props('outlined dark dense').classes('w-full max-w-md')
            desc_input = ui.input(
                t('Beschreibung'), value=grp.description or ''
            ).props('outlined dark dense').classes('w-full max-w-md')

            # ── Permissions ───────────────────────────────────────────────────
            ui.label(t('Berechtigungen')).classes('text-xs font-bold text-zinc-400 uppercase tracking-wider mt-2')
            ui.label(
                t('Strings der Form "%{example}". In Views / API-Guards nutzbar mit '
                  'group_service.user_has_permission(roles, "%{example}").', example='bereich.aktion')
            ).classes('text-[10px] text-zinc-600')

            perm_input = ui.input(
                placeholder=t('z.B. dashboard.view')
            ).props('outlined dark dense').classes('w-full max-w-md')

            perms_container = ui.row().classes('flex-wrap gap-1 mt-1')
            current_perms: list = list(grp.permissions or [])

            def _render_perms():
                perms_container.clear()
                with perms_container:
                    for p in current_perms:
                        with ui.row().classes(
                            'items-center gap-1 bg-teal-500/15 border border-teal-500/30 '
                            'rounded-full px-2 py-0.5'
                        ):
                            ui.label(p).classes('text-[11px] font-mono text-teal-300')
                            ui.button(
                                icon='close',
                                on_click=lambda _, perm=p: _remove_perm(perm),
                            ).props('flat round dense').classes('text-teal-600 w-4 h-4')

            def _add_perm():
                val = perm_input.value.strip()
                if val and val not in current_perms:
                    current_perms.append(val)
                    _render_perms()
                perm_input.set_value('')

            def _remove_perm(perm: str):
                if perm in current_perms:
                    current_perms.remove(perm)
                _render_perms()

            _render_perms()
            perm_input.on('keydown.enter', lambda _: _add_perm())
            ui.button(t('Hinzufügen'), icon='add', on_click=_add_perm).props(
                'outline size=sm color=teal'
            )

            # ── Suggestion chips ──────────────────────────────────────────────
            with ui.row().classes('flex-wrap gap-1 mt-1'):
                for suggestion in _SUGGESTED_PERMISSIONS:
                    ui.button(
                        suggestion,
                        on_click=lambda _, s=suggestion: [
                            current_perms.append(s) if s not in current_perms else None,
                            _render_perms(),
                        ],
                    ).props('outline dense size=xs').classes(
                        'text-[10px] font-mono text-zinc-500 '
                        + ('opacity-40' if suggestion in current_perms else '')
                    )

            # ── LDAP Mappings ─────────────────────────────────────────────────
            ui.label(t('LDAP-Gruppen-Mapping')).classes(
                'text-xs font-bold text-zinc-400 uppercase tracking-wider mt-3'
            )
            ui.label(
                t('LDAP-Gruppen-DNs, deren Mitglieder diese Gruppe beim Login erhalten. '
                  'z.B. cn=ops,ou=groups,dc=example,dc=com')
            ).classes('text-[10px] text-zinc-600')

            ldap_input = ui.input(
                placeholder=t('cn=ops,ou=groups,dc=example,dc=com')
            ).props('outlined dark dense').classes('w-full max-w-md font-mono')

            ldap_container = ui.row().classes('flex-wrap gap-1 mt-1')
            current_ldap: list = list(grp.ldap_mappings or [])

            def _render_ldap():
                ldap_container.clear()
                with ldap_container:
                    for dn in current_ldap:
                        with ui.row().classes(
                            'items-center gap-1 bg-sky-500/15 border border-sky-500/30 '
                            'rounded-full px-2 py-0.5'
                        ):
                            ui.label(dn).classes('text-[11px] font-mono text-sky-300 max-w-xs truncate')
                            ui.button(
                                icon='close',
                                on_click=lambda _, d=dn: _remove_ldap(d),
                            ).props('flat round dense').classes('text-sky-600 w-4 h-4')

            def _add_ldap():
                val = ldap_input.value.strip()
                if val and val not in current_ldap:
                    current_ldap.append(val)
                    _render_ldap()
                ldap_input.set_value('')

            def _remove_ldap(dn: str):
                if dn in current_ldap:
                    current_ldap.remove(dn)
                _render_ldap()

            _render_ldap()
            ldap_input.on('keydown.enter', lambda _: _add_ldap())
            ui.button(t('Hinzufügen'), icon='add', on_click=_add_ldap).props(
                'outline size=sm color=sky'
            )

            # ── Mitglieder ────────────────────────────────────────────────────
            ui.label(t('Mitglieder')).classes(
                'text-xs font-bold text-zinc-400 uppercase tracking-wider mt-3'
            )
            ui.label(
                t('Benutzer, die dieser Gruppe explizit zugewiesen sind. '
                  'LDAP-Benutzer erhalten die Gruppe automatisch beim Login über das LDAP-Mapping.')
            ).classes('text-[10px] text-zinc-600')

            members_container = ui.column().classes('gap-1 mt-1')

            def _render_members():
                members_container.clear()
                members = group_service.get_group_members(grp.id)
                with members_container:
                    if not members:
                        ui.label(t('Keine direkten Mitglieder.')).classes(
                            'text-xs text-zinc-600 italic py-1'
                        )
                    for member in members:
                        with ui.row().classes(
                            'items-center gap-2 bg-zinc-800/40 border border-zinc-700/30 '
                            'rounded-lg px-3 py-1.5'
                        ):
                            ui.icon('person', size='14px').classes('text-zinc-500 shrink-0')
                            ui.label(member.username).classes(
                                'text-xs font-semibold text-zinc-200 flex-grow'
                            )
                            if member.full_name and member.full_name != member.username:
                                ui.label(member.full_name).classes('text-[10px] text-zinc-500')
                            ui.button(
                                icon='person_remove',
                                on_click=lambda _, m=member: _remove_member(m.username),
                            ).props('flat round dense size=xs').classes('text-zinc-600 hover:text-red-400')

            def _remove_member(uname: str):
                group_service.remove_user_from_group(grp.id, uname)
                _render_members()

            _render_members()

            # Add member
            from core.components.auth.logic.user_service import user_service as _user_svc
            all_users = _user_svc.get_all()
            current_members = {m.username for m in group_service.get_group_members(grp.id)}
            non_members = [u.username for u in all_users if u.username not in current_members]

            with ui.row().classes('items-center gap-2 mt-2'):
                member_select = ui.select(
                    options=non_members,
                    label=t('Benutzer hinzufügen'),
                ).props('outlined dark dense').classes('flex-grow max-w-xs')

                def _add_member():
                    if not member_select.value:
                        return
                    group_service.add_user_to_group(grp.id, member_select.value)
                    _render_members()
                    # Refresh dropdown options
                    new_members = {m.username for m in group_service.get_group_members(grp.id)}
                    member_select.set_options(
                        [u.username for u in all_users if u.username not in new_members]
                    )
                    member_select.set_value(None)

                ui.button(icon='person_add', on_click=_add_member).props(
                    'unelevated size=sm color=teal'
                ).tooltip(t('Zum Mitglied machen'))

            # ── Save / Delete row ─────────────────────────────────────────────
            with ui.row().classes('w-full items-center gap-3 mt-4 pt-3 border-t border-zinc-800/40'):
                def save_group():
                    group_service.update(
                        grp.id,
                        name=name_input.value.strip() or grp.name,
                        description=desc_input.value,
                        permissions=list(current_perms),
                        ldap_mappings=list(current_ldap),
                    )
                    ui.notify(t('Gruppe gespeichert.'), type='positive')
                    _refresh_list()
                    editor_col.clear()
                    selected_id["value"] = None

                def delete_group():
                    gname = grp.name
                    group_service.delete(grp.id)
                    ui.notify(t('Gruppe "%{name}" gelöscht.', name=gname), type='positive')
                    editor_col.clear()
                    selected_id["value"] = None
                    _refresh_list()

                ui.button(t('Speichern'), icon='save', on_click=save_group).props(
                    'unelevated size=sm color=primary'
                )
                ui.space()
                ui.button(t('Löschen'), icon='delete', on_click=delete_group).props(
                    'outline size=sm color=negative'
                )

        def _refresh_list() -> None:
            group_list_col.clear()
            groups = group_service.get_all()
            if not groups:
                with group_list_col:
                    ui.label(t('Keine Gruppen vorhanden.')).classes(
                        'text-xs text-zinc-600 italic p-2'
                    )
                return
            with group_list_col:
                for grp in groups:
                    is_selected = (grp.id == selected_id["value"])
                    base_cls = (
                        'w-full text-left px-3 py-2 rounded-lg text-sm font-medium transition-colors '
                        'cursor-pointer '
                    )
                    active_cls = 'bg-teal-500/20 text-teal-200 border border-teal-500/30'
                    inactive_cls = 'text-zinc-300 hover:bg-zinc-800/50'
                    with ui.row().classes(
                        base_cls + (active_cls if is_selected else inactive_cls)
                    ).on('click', lambda _, g=grp: _open_editor(g)):
                        with ui.column().classes('gap-0 flex-grow overflow-hidden'):
                            ui.label(grp.name).classes('text-sm font-semibold truncate')
                            perm_count = len(grp.permissions or [])
                            ldap_count = len(grp.ldap_mappings or [])
                            ui.label(
                                t('%{perm_count} Rechte · %{ldap_count} LDAP-Mappings', perm_count=perm_count, ldap_count=ldap_count)
                            ).classes('text-[10px] text-zinc-500')

        # Initial list render
        _refresh_list()
