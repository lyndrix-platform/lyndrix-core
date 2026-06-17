import asyncio
from nicegui import ui
from ui.theme import UIStyles
from core.components.plugins.logic.manager import module_manager
from core.services import monitor_service, vault_instance, db_instance

async def render_dashboard_page():
    """Renders the main dashboard in a modern vertical stack."""

    with ui.column().classes('w-full max-w-7xl mx-auto gap-8 py-4'):
        # --- HEADER ---
        with ui.row().classes('w-full items-center gap-4'):
            ui.element('div').classes('h-12 w-1 bg-gradient-to-b from-indigo-400 to-sky-400')
            with ui.column().classes('gap-0 flex-grow'):
                ui.label('System Overview').classes(UIStyles.TITLE_H2)
                ui.label('Real-time metrics and subsystem status.').classes(UIStyles.TEXT_MUTED)
            ui.button(icon='refresh', on_click=lambda: ui.run_javascript('window.location.reload()')).props('flat round color=zinc-500').tooltip('Refresh Dashboard')

        # --- STACK 1: CORE SUBSYSTEMS ---
        with ui.row().classes('items-center gap-2'):
            ui.element('div').classes('h-5 w-0.5 bg-gradient-to-b from-indigo-400 to-violet-400')
            ui.label('Core Infrastructure').classes(UIStyles.TITLE_H3)
        with ui.grid(columns='repeat(auto-fit, minmax(250px, 1fr))').classes('w-full gap-4'):
            # Vault Status
            with ui.card().classes(f'{UIStyles.CARD_GLASS}').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-indigo-400 via-violet-400 to-purple-400')
                with ui.row().classes('items-center gap-4 p-4'):
                    ui.icon('lock', size='28px').classes('text-indigo-400 shrink-0')
                    with ui.column().classes('gap-0'):
                        ui.label('Vault Service').classes(UIStyles.TITLE_H3)
                        if vault_instance.is_connected:
                            ui.label('Online & Unsealed').classes('text-xs text-emerald-400 font-mono')
                        else:
                            ui.label(vault_instance.ui_state.replace('_', ' ').title()).classes('text-xs text-amber-400 font-mono')

            # Database Status
            with ui.card().classes(f'{UIStyles.CARD_GLASS}').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-sky-400 via-blue-400 to-cyan-400')
                with ui.row().classes('items-center gap-4 p-4'):
                    ui.icon('storage', size='28px').classes('text-sky-400 shrink-0')
                    with ui.column().classes('gap-0'):
                        ui.label('MariaDB Engine').classes(UIStyles.TITLE_H3)
                        if db_instance.is_connected:
                            ui.label('Connected').classes('text-xs text-emerald-400 font-mono')
                        else:
                            ui.label('Disconnected').classes('text-xs text-red-400 font-mono')

            # Plugin Manager Status
            active_plugins = sum(1 for p in module_manager.registry.values() if p.get("status") == "active" and p["manifest"].type == "PLUGIN")
            total_plugins = sum(1 for p in module_manager.registry.values() if p["manifest"].type == "PLUGIN")

            with ui.card().classes(f'{UIStyles.CARD_GLASS}').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 via-teal-400 to-green-400')
                with ui.row().classes('items-center gap-4 p-4'):
                    ui.icon('extension', size='28px').classes('text-emerald-400 shrink-0')
                    with ui.column().classes('gap-0'):
                        ui.label('Plugin Subsystem').classes(UIStyles.TITLE_H3)
                        ui.label(f'{active_plugins} / {total_plugins} Active').classes('text-xs text-emerald-400 font-mono')

        # --- STACK 2: HARDWARE METRICS ---
        with ui.row().classes('items-center gap-2 mt-4'):
            ui.element('div').classes('h-5 w-0.5 bg-gradient-to-b from-sky-400 to-cyan-400')
            ui.label('Hardware Utilization').classes(UIStyles.TITLE_H3)
        with ui.grid(columns='repeat(auto-fit, minmax(300px, 1fr))').classes('w-full gap-4'):
            # CPU
            with ui.card().classes(f'{UIStyles.CARD_GLASS}').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-indigo-400 via-violet-400 to-indigo-600')
                with ui.column().classes('flex-grow p-5 gap-2'):
                    with ui.row().classes('w-full justify-between items-center'):
                        ui.label('CPU Usage').classes(UIStyles.LABEL_MINI)
                        ui.icon('memory', size='18px').classes('text-indigo-400')
                    cpu_prog = ui.linear_progress(value=0, show_value=False).props('color=indigo size=6px')
                    ui.label().classes('text-3xl font-black font-mono text-indigo-400').bind_text_from(monitor_service.stats, 'cpu', lambda v: f"{v:.1f}%")
                    cpu_prog.bind_value_from(monitor_service.stats, 'cpu', lambda v: v / 100.0)

            # RAM
            with ui.card().classes(f'{UIStyles.CARD_GLASS}').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 via-teal-400 to-green-500')
                with ui.column().classes('flex-grow p-5 gap-2'):
                    with ui.row().classes('w-full justify-between items-center'):
                        ui.label('Memory (RAM)').classes(UIStyles.LABEL_MINI)
                        ui.icon('dns', size='18px').classes('text-emerald-400')
                    ram_prog = ui.linear_progress(value=0, show_value=False).props('color=emerald size=6px')
                    ui.label().classes('text-3xl font-black font-mono text-emerald-400').bind_text_from(monitor_service.stats, 'ram', lambda v: f"{v:.1f}%")
                    ram_prog.bind_value_from(monitor_service.stats, 'ram', lambda v: v / 100.0)

            # DISK
            with ui.card().classes(f'{UIStyles.CARD_GLASS}').style('padding: 0; flex-wrap: nowrap'):
                ui.element('div').classes('h-1 w-full bg-gradient-to-r from-amber-400 via-orange-400 to-yellow-500')
                with ui.column().classes('flex-grow p-5 gap-2'):
                    with ui.row().classes('w-full justify-between items-center'):
                        ui.label('Disk IO').classes(UIStyles.LABEL_MINI)
                        ui.icon('save', size='18px').classes('text-amber-400')
                    disk_prog = ui.linear_progress(value=0, show_value=False).props('color=amber size=6px')
                    ui.label().classes('text-3xl font-black font-mono text-amber-400').bind_text_from(monitor_service.stats, 'disk', lambda v: f"{v:.1f}%")
                    disk_prog.bind_value_from(monitor_service.stats, 'disk', lambda v: v / 100.0)

        # --- STACK 3: PLUGIN WIDGETS ---
        with ui.row().classes('items-center gap-2 mt-4'):
            ui.element('div').classes('h-5 w-0.5 bg-gradient-to-b from-rose-400 to-pink-400')
            ui.label('Module Integrations').classes(UIStyles.TITLE_H3)
        with ui.grid(columns='repeat(auto-fit, minmax(350px, 1fr))').classes('w-full gap-6'):
            widget_rendered = False
            for entry in module_manager.registry.values():
                if entry.get("status") == "active" and hasattr(entry["module"], 'render_dashboard_widget'):
                    widget_rendered = True
                    try:
                        with ui.card().classes(f'{UIStyles.CARD_GLASS} flex flex-col').style('padding: 0; flex-wrap: nowrap'):
                            ui.element('div').classes('h-1 w-full bg-gradient-to-r from-rose-400 via-pink-400 to-indigo-400')
                            with ui.column().classes('flex-grow p-5 gap-2'):
                                widget_render_func = entry["module"].render_dashboard_widget
                                if asyncio.iscoroutinefunction(widget_render_func):
                                    await widget_render_func(entry["context"])
                                else:
                                    widget_render_func(entry["context"])
                    except Exception as e:
                        with ui.card().classes(f'{UIStyles.CARD_GLASS}').style('padding: 0; flex-wrap: nowrap'):
                            ui.element('div').classes('h-1 w-full bg-gradient-to-r from-red-400 to-rose-500')
                            with ui.column().classes('flex-grow p-4 gap-1'):
                                ui.label(f"Widget Error: {entry['manifest'].name}").classes('text-red-400 font-bold text-sm')
                                ui.label(str(e)).classes('text-red-500 text-xs font-mono')

            if not widget_rendered:
                ui.label('No active plugins provide dashboard widgets.').classes(f'{UIStyles.TEXT_MUTED} italic col-span-full mt-2')