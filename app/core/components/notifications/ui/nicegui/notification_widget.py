from nicegui import ui
from core.api import UIStyles
from core.i18n import t
from ...logic.notification_service import notification_service

# type → (text colour, background, icon). ``info`` is the default fallback.
_TYPE_STYLES: dict[str, tuple[str, str, str]] = {
    "positive": ("text-emerald-400", "bg-emerald-500/10", "check_circle"),
    "negative": ("text-red-400", "bg-red-500/10", "error"),
    "warning": ("text-amber-400", "bg-amber-500/10", "warning"),
    "ongoing": ("text-indigo-400", "bg-indigo-500/10", "sync"),
    "info": ("text-blue-400", "bg-blue-500/10", "info"),
}


def render_notification_bell(user_id: str = "admin"):
    last_state_hash = None

    with ui.button(icon='notifications').props('flat round color=slate-300') as btn:
        badge = ui.badge('', color='red').props('floating').classes('hidden')
        
        with ui.menu().classes(f'lyndrix-notif-popup max-h-[75vh] p-0 flex flex-col {UIStyles.MENU_CONTAINER}').style('width: min(24rem, calc(100vw - 1rem))').props('anchor="bottom right" self="top right" transition-show="jump-down" transition-hide="jump-up"'):
            with ui.row().classes('w-full justify-between items-center p-3 border-b border-zinc-800 bg-zinc-900 shrink-0'):
                ui.label(t('notifications.title')).classes('text-sm font-bold text-slate-200 tracking-wide')
                ui.button(t('notifications.clear_all'), on_click=lambda: notification_service.clear_for_user(user_id)).props('flat dense size=xs color=zinc-400')
            
            list_container = ui.column().classes('w-full p-2 gap-2 overflow-y-auto flex-nowrap max-h-[calc(75vh-56px)]')

    def update_ui():
        nonlocal last_state_hash
        unread = notification_service.unread_for_user(user_id)
        count = len(unread)
        
        if count > 0:
            badge.set_text(str(count))
            badge.classes(remove='hidden')
        else:
            badge.classes(add='hidden')
            
        # Create a hash of the current unread state to avoid unnecessary DOM updates and flickering.
        current_hash = hash(str([(n['id'], n['type'], n['title'], n['message']) for n in unread]))
        if current_hash == last_state_hash:
            return
        last_state_hash = current_hash

        list_container.clear()
        with list_container:
            if count == 0:
                ui.label(t('notifications.quiet')).classes('text-xs text-zinc-500 p-4 text-center w-full')
                
            for n in unread:
                # Styling based on state type (info is the default fallback).
                color, bg, icon = _TYPE_STYLES.get(n['type'], _TYPE_STYLES['info'])

                with ui.card().classes(f'w-full {bg} border border-zinc-800/50 p-2 gap-0 shadow-none'):
                    with ui.row().classes('w-full items-start flex-nowrap gap-2'):
                        if n['type'] == 'ongoing':
                            ui.spinner('tail', size='1em', color='indigo').classes('mt-0.5 shrink-0')
                        else:
                            ui.icon(icon, size='16px').classes(f'{color} mt-0.5 shrink-0')
                        
                        with ui.column().classes('gap-0 flex-grow'):
                            ui.label(n['title']).classes(f'text-xs font-bold {color} leading-tight')
                            ui.label(n['message']).classes('text-[11px] text-slate-300 mt-0.5 break-words leading-snug whitespace-pre-wrap')
                            
                        with ui.column().classes('shrink-0 gap-0 -mt-1'):
                            ui.button(icon='notifications_paused', on_click=lambda _, nid=n['id']: notification_service.mark_read(user_id, nid)).props('flat round dense size=xs color=zinc-500').tooltip(t('notifications.mute'))
                            ui.button(icon='close', on_click=lambda _, nid=n['id']: notification_service.dismiss(user_id, nid)).props('flat round dense size=xs color=zinc-500').tooltip(t('notifications.dismiss'))

    # TODO(agent): drive bell updates from the ``notification:new`` bus event / SSE
    # and skip work when the tab is hidden, instead of this fixed 2s poll per client.
    ui.timer(2.0, update_ui)
    return btn
