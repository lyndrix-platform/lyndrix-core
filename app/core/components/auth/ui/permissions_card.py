"""
Permissions management card for the Settings UI.

Shows all known permissions (auto-scanned from manifests + built-in features)
grouped by category.  For each permission you can see which groups have it and
which users have a direct grant, and add / remove assignments.

Changes are applied immediately:
  - Group assignment  → group_service.update() adds/removes the perm from Group.permissions
  - User direct grant → user_service.add/remove_extra_permission()
"""
from nicegui import ui

from core.logger import get_logger
from core.i18n import t
from ui.theme import UIStyles

log = get_logger("UI:PermissionsCard")


async def render_permissions_card() -> None:
    from core.components.auth.logic.permission_registry import permission_registry, PermissionDef
    from core.components.auth.logic.group_service import group_service
    from core.components.auth.logic.user_service import user_service

    # Refresh permissions from active manifests each time the tab is opened
    permission_registry.scan_from_manifests()

    with ui.column().classes('w-full gap-4'):

        # ── Header info ───────────────────────────────────────────────────────
        with ui.row().classes(
            'w-full items-start gap-3 p-4 '
            'bg-zinc-800/30 border border-zinc-700/30 rounded-xl'
        ):
            ui.icon('info_outline', size='18px').classes('text-zinc-500 shrink-0 mt-0.5')
            ui.label(
                t('Berechtigungen werden automatisch aus aktiven Modulen und Plugins geladen. '
                  'Gruppen-Zuweisungen wirken beim nächsten Login. '
                  'Direkte Nutzer-Zuweisungen gelten sofort (nach erneutem Login).')
            ).classes('text-xs text-zinc-500')

        # ── Per-category cards ────────────────────────────────────────────────
        for category in permission_registry.categories():
            perms = permission_registry.get_by_category(category)
            if not perms:
                continue

            label_text = permission_registry.category_label(category)
            color_cls  = permission_registry.category_color(category)

            # Map gradient by category
            grad = {
                "route":   "from-sky-400 to-cyan-400",
                "plugin":  "from-violet-400 to-purple-400",
                "feature": "from-emerald-400 to-teal-400",
                "api":     "from-amber-400 to-orange-400",
            }.get(category, "from-zinc-500 to-zinc-600")

            with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style(
                'padding: 0; flex-wrap: nowrap'
            ):
                ui.element('div').classes(f'h-1 w-full bg-gradient-to-r {grad}')
                with ui.column().classes('w-full flex-grow p-5 gap-0'):
                    with ui.row().classes('items-center gap-2 mb-3'):
                            ui.icon('verified_user', size='18px').classes(color_cls)
                            ui.label(label_text).classes(UIStyles.TITLE_H3)

                    for pdef in perms:
                        _render_permission_row(pdef, group_service, user_service)


def _render_permission_row(
    pdef,
    group_service,
    user_service,
) -> None:
    """Render one permission row with expandable assignment panel."""

    # Pre-compute which groups and users have this permission
    all_groups = group_service.get_all()
    all_users  = user_service.get_all()

    groups_with_perm = [g for g in all_groups if pdef.id in (g.permissions or [])]
    users_with_perm  = [u for u in all_users  if pdef.id in (u.extra_permissions or [])]
    groups_without   = [g for g in all_groups if pdef.id not in (g.permissions or [])]
    users_without    = [u for u in all_users  if pdef.id not in (u.extra_permissions or [])]

    with ui.expansion(
        pdef.label,
        icon=pdef.icon,
        caption=pdef.id,
    ).classes(
        'w-full border-b border-zinc-800/30 last:border-0 text-zinc-200'
    ):
        with ui.column().classes('w-full gap-4 pb-2 pt-1 pl-8'):

            # ── Current group assignments ─────────────────────────────────────
            with ui.column().classes('w-full gap-1'):
                ui.label(t('Gruppen mit dieser Berechtigung')).classes(
                    'text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-1'
                )
                grp_container = ui.row().classes('flex-wrap gap-1')

                def _render_grp_chips(container=grp_container, perm_id=pdef.id):
                    container.clear()
                    _groups = [g for g in group_service.get_all() if perm_id in (g.permissions or [])]
                    with container:
                        if not _groups:
                            ui.label(t('Keine Gruppe zugewiesen.')).classes(
                                'text-xs text-zinc-600 italic py-1'
                            )
                        for grp in _groups:
                            with ui.row().classes(
                                'items-center gap-1 bg-teal-500/15 border border-teal-500/30 '
                                'rounded-full px-2 py-0.5'
                            ):
                                ui.label(grp.name).classes('text-[11px] font-mono text-teal-300')
                                ui.button(
                                    icon='close',
                                    on_click=lambda _, g=grp: _remove_grp_perm(g, perm_id, container),
                                ).props('flat round dense').classes('text-teal-600 w-4 h-4')

                def _remove_grp_perm(grp, perm_id, container):
                    perms = [p for p in (grp.permissions or []) if p != perm_id]
                    group_service.update(grp.id, permissions=perms)
                    _render_grp_chips(container, perm_id)

                _render_grp_chips()

                # Add group
                if groups_without:
                    with ui.row().classes('items-center gap-2 mt-1'):
                        grp_select = ui.select(
                            options=[g.name for g in group_service.get_all()
                                     if pdef.id not in (g.permissions or [])],
                            label=t('Gruppe hinzufügen'),
                        ).props('outlined dark dense').classes('max-w-xs')

                        def _add_grp_perm(perm_id=pdef.id, sel=grp_select, container=grp_container):
                            if not sel.value:
                                return
                            grp = group_service.get_by_name(sel.value)
                            if grp:
                                perms = list(grp.permissions or [])
                                if perm_id not in perms:
                                    perms.append(perm_id)
                                    group_service.update(grp.id, permissions=perms)
                                _render_grp_chips(container, perm_id)
                                sel.set_options(
                                    [g.name for g in group_service.get_all()
                                     if perm_id not in (g.permissions or [])]
                                )
                                sel.set_value(None)

                        ui.button(icon='add', on_click=_add_grp_perm).props(
                            'unelevated size=sm color=teal'
                        ).tooltip(t('Gruppe diese Berechtigung geben'))

            # ── Direct user grants ────────────────────────────────────────────
            with ui.column().classes('w-full gap-1'):
                ui.label(t('Direkte Nutzer-Zuweisungen')).classes(
                    'text-[10px] font-bold uppercase tracking-wider text-zinc-500 mb-1'
                )
                usr_container = ui.row().classes('flex-wrap gap-1')

                def _render_usr_chips(container=usr_container, perm_id=pdef.id):
                    container.clear()
                    _users = [u for u in user_service.get_all()
                              if perm_id in (u.extra_permissions or [])]
                    with container:
                        if not _users:
                            ui.label(t('Keine direkte Zuweisung.')).classes(
                                'text-xs text-zinc-600 italic py-1'
                            )
                        for usr in _users:
                            with ui.row().classes(
                                'items-center gap-1 bg-indigo-500/15 border border-indigo-500/30 '
                                'rounded-full px-2 py-0.5'
                            ):
                                ui.label(usr.username).classes('text-[11px] font-mono text-indigo-300')
                                ui.button(
                                    icon='close',
                                    on_click=lambda _, u=usr: _remove_usr_perm(u, perm_id, container),
                                ).props('flat round dense').classes('text-indigo-600 w-4 h-4')

                def _remove_usr_perm(usr, perm_id, container):
                    user_service.remove_extra_permission(usr.username, perm_id)
                    _render_usr_chips(container, perm_id)

                _render_usr_chips()

                # Add user
                non_granted_users = [u.username for u in user_service.get_all()
                                     if pdef.id not in (u.extra_permissions or [])]
                if non_granted_users:
                    with ui.row().classes('items-center gap-2 mt-1'):
                        usr_select = ui.select(
                            options=non_granted_users,
                            label=t('Benutzer direkt hinzufügen'),
                        ).props('outlined dark dense').classes('max-w-xs')

                        def _add_usr_perm(perm_id=pdef.id, sel=usr_select, container=usr_container):
                            if not sel.value:
                                return
                            user_service.add_extra_permission(sel.value, perm_id)
                            _render_usr_chips(container, perm_id)
                            sel.set_options(
                                [u.username for u in user_service.get_all()
                                 if perm_id not in (u.extra_permissions or [])]
                            )
                            sel.set_value(None)

                        ui.button(icon='person_add', on_click=_add_usr_perm).props(
                            'unelevated size=sm color=indigo'
                        ).tooltip(t('Direkte Berechtigung vergeben'))
