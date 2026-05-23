import inspect
from functools import wraps
from nicegui import ui

from config import settings
from ui.theme import apply_theme, UIStyles, set_page_metadata
from ui.maintenance import attach_maintenance_overlay
from core.logger import get_logger
from core.i18n import t, set_locale, get_locale
from core.bus import bus
from core.session import (
    clear_user_session,
    get_user_value,
    is_authenticated,
    set_user_value,
)
from core.components.plugins.logic.manager import module_manager
from core.components.notifications.notification_widget import render_notification_bell

log = get_logger("UI:Layout")


_ui_refresh_subscription_registered = False


def _safe_user_value(key: str, default=None):
    return get_user_value(key, default)


# ==========================================
# ZENTRALE SETTINGS POPUP LOGIK
# ==========================================


# ==========================================
# RESTLICHE LAYOUT FUNKTIONEN
# ==========================================
def trigger_reload():
    """Triggers a client-side page reload."""
    ui.notify("Refreshing UI...", type="info")
    ui.run_javascript("window.location.reload();")


def _register_ui_refresh_subscription():
    global _ui_refresh_subscription_registered
    if _ui_refresh_subscription_registered:
        return

    @bus.subscribe("ui:needs_refresh")
    def on_ui_refresh(payload):
        reason = (payload or {}).get("reason", "unknown reason")
        # NiceGUI slot-bound APIs cannot be called safely from this global bus callback.
        # Browsers reconnect or users can refresh manually; the critical part is avoiding
        # repeated background-task crashes during plugin lifecycle changes.
        log.info(f"UI refresh requested: {reason}")

    _ui_refresh_subscription_registered = True


def logout():
    clear_user_session()
    ui.navigate.to("/login")


def get_nav_items():
    """Generates navigation links dynamically from module manifests."""
    core_items = []
    plugin_items = []

    manifests = module_manager.get_manifests()

    for manifest in manifests:
        # NEW: Only show active modules in the navigation
        entry = module_manager.registry.get(manifest.id, {})
        if entry.get("status") != "active":
            continue

        route = getattr(manifest, "ui_route", None)
        if not route:
            continue

        item = {
            "id": manifest.id,
            "label": manifest.name,
            "icon": manifest.icon or "extension",
            "target": route,
            "type": manifest.type,
        }

        if manifest.type == "CORE":
            core_items.append(item)
        else:
            plugin_items.append(item)

    core_items.append(
        {
            "id": "core.settings",
            "label": "Settings",
            "icon": "settings",
            "target": "/settings",
            "type": "CORE",
        }
    )
    return core_items, plugin_items


def open_plugin_settings_modal(manifest_id: str):
    """Sucht das Modul in der Registry und öffnet dessen Settings-UI in einem gestylten Dialog."""
    entry = module_manager.registry.get(manifest_id)
    if not entry or entry.get("status") != "active":
        ui.notify("Fehler: Modul ist nicht aktiv oder nicht gefunden.", type="negative")
        return

    manifest = entry["manifest"]
    mod = entry["module"]
    ctx = entry["context"]

    if not hasattr(mod, "render_settings_ui"):
        ui.notify(
            f"{manifest.name} hat keine konfigurierbaren Einstellungen.", type="info"
        )
        return

    with ui.dialog() as settings_dialog, ui.card().classes(
        f"w-[calc(100vw-40px)] h-[calc(100vh-40px)] max-w-none max-h-none p-[20px] flex flex-col {UIStyles.MODAL_CONTAINER}"
    ):

        with ui.row().classes("w-full justify-between items-center mb-4 shrink-0"):
            with ui.row().classes("items-center gap-3"):
                ui.icon(manifest.icon, size="24px").classes("text-primary")
                with ui.column().classes("gap-0"):
                    ui.label(manifest.name).classes(
                        "font-bold text-lg leading-tight text-slate-800 dark:text-zinc-100"
                    )
                    ui.label(f"v{manifest.version}").classes(
                        "text-xs font-mono text-slate-500 dark:text-zinc-400"
                    )

            ui.button(icon="close", on_click=settings_dialog.close).props(
                "flat round dense"
            ).classes(
                "text-slate-500 hover:text-slate-800 dark:hover:text-white transition-colors"
            )

        with ui.scroll_area().classes("w-full flex-grow pr-4"):
            try:
                mod.render_settings_ui(ctx)
            except Exception as e:
                ui.label(f"Fehler in Plugin-UI: {str(e)}").classes(
                    "text-red-500 text-xs"
                )

    settings_dialog.open()


def main_layout(page_title: str):
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            _register_ui_refresh_subscription()
            if not is_authenticated():
                ui.navigate.to("/login")
                return

            theme_pref = _safe_user_value("theme_pref", "dark")
            theme_id = _safe_user_value("theme_id", settings.DEFAULT_THEME_ID)
            apply_theme(theme_pref, page_title=page_title, theme_id=theme_id)
            set_page_metadata(page_title)

            is_dark = theme_pref == "dark"
            dark = ui.dark_mode(value=is_dark)

            ui.run_javascript(
                f"document.documentElement.classList.toggle('dark', {'true' if is_dark else 'false'});"
            )

            def on_theme_switch(e):
                mode = "dark" if e.value else "light"
                set_user_value("theme_pref", mode)
                if e.value:
                    dark.enable()
                else:
                    dark.disable()
                ui.run_javascript(
                    f"document.documentElement.classList.toggle('dark', {'true' if e.value else 'false'});"
                )

            attach_maintenance_overlay()
            core_items, plugin_items = get_nav_items()

            # Finde die Manifest-ID der aktuellen Seite
            active_manifest_id = None
            for item in plugin_items + core_items:
                if item["label"] == page_title:
                    active_manifest_id = item["id"]
                    break

            # --- HEADER ---
            with ui.header(elevated=False).classes(UIStyles.HEADER).props("dense"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.button(icon="menu").props(
                        "flat round dense text-color=current"
                    ).on("click", lambda: left_drawer.toggle())
                    ui.label(settings.APP_TITLE).classes(
                        "text-xs font-bold tracking-widest uppercase text-primary"
                    )
                    ui.space()

                    with ui.row().classes("items-center gap-2"):

                        # --- DYNAMISCHER SETTINGS BUTTON ---
                        if active_manifest_id and active_manifest_id != "core.settings":
                            entry = module_manager.registry.get(active_manifest_id)
                            if entry and hasattr(
                                entry.get("module"), "render_settings_ui"
                            ):
                                # FIX: text-color=current passt sich dem Theme an!
                                ui.button(
                                    icon="settings_applications",
                                    on_click=lambda: open_plugin_settings_modal(
                                        active_manifest_id
                                    ),
                                ).props("flat round text-color=current").tooltip(
                                    f"{page_title} Einstellungen"
                                )
                                ui.separator().props("vertical").classes(
                                    "mx-1 h-6 self-center opacity-30"
                                )

                        with ui.row().classes(
                            "items-center bg-slate-100 dark:bg-zinc-800 px-2 py-0.5 rounded-full border border-slate-200 dark:border-zinc-700 mr-2"
                        ):
                            ui.icon("light_mode", size="14px").classes(
                                "text-orange-500"
                            )
                            ui.switch(value=is_dark, on_change=on_theme_switch).props(
                                "dense color=primary"
                            )
                            ui.icon("dark_mode", size="14px").classes("text-indigo-400")

                        ui.label(page_title).classes(UIStyles.LABEL_MINI)
                        render_notification_bell()
                        with ui.button(icon="account_circle").props(
                            "flat round text-color=current"
                        ):
                            with ui.menu().classes(
                                f"w-64 p-0 flex flex-col {UIStyles.MENU_CONTAINER}"
                            ):
                                with ui.row().classes(
                                    "w-full items-center p-4 border-b border-zinc-800 bg-zinc-900 shrink-0 gap-3"
                                ):
                                    ui.icon(
                                        "admin_panel_settings", size="28px"
                                    ).classes("text-indigo-400")
                                    with ui.column().classes("gap-0"):
                                        username = _safe_user_value(
                                            "username", "Administrator"
                                        )
                                        ui.label(username).classes(
                                            "text-sm font-bold text-slate-200 tracking-wide capitalize"
                                        )
                                        ui.label("System Owner").classes(
                                            "text-[10px] text-slate-500 uppercase tracking-widest"
                                        )

                                with ui.column().classes("w-full p-2 gap-1"):
                                    # --- Language switcher ---
                                    supported = [
                                        s.strip()
                                        for s in settings.SUPPORTED_LOCALES.split(",")
                                        if s.strip()
                                    ]
                                    locale_labels = {
                                        "en": "🇬🇧 English",
                                        "de": "🇩🇪 Deutsch",
                                    }
                                    current_locale = get_locale()

                                    def on_locale_change(e):
                                        set_locale(e.value)
                                        ui.run_javascript("window.location.reload()")

                                    ui.select(
                                        options={
                                            loc: locale_labels.get(loc, loc.upper())
                                            for loc in supported
                                        },
                                        value=current_locale,
                                        on_change=on_locale_change,
                                    ).props("dense outlined dark borderless").classes(
                                        "w-full text-xs mb-1"
                                    )

                                    ui.separator().classes("bg-zinc-800 my-1")

                                    with ui.button(on_click=trigger_reload).props(
                                        "flat dense"
                                    ).classes(
                                        "w-full justify-start px-3 py-2 text-slate-300 hover:bg-zinc-800 hover:text-white rounded transition-colors"
                                    ):
                                        ui.icon("refresh", size="16px").classes(
                                            "mr-2 text-slate-400"
                                        )
                                        ui.label(t("core.header.refresh_ui")).classes(
                                            "text-xs font-bold capitalize"
                                        )

                                    with ui.button(on_click=logout).props(
                                        "flat dense"
                                    ).classes(
                                        "w-full justify-start px-3 py-2 text-red-400 hover:bg-red-500/10 hover:text-red-300 rounded transition-colors mt-1"
                                    ):
                                        ui.icon("logout", size="16px").classes("mr-2")
                                        ui.label(t("core.header.logout")).classes(
                                            "text-xs font-bold capitalize"
                                        )

            # --- SIDEBAR ---
            with ui.left_drawer(value=False).classes(UIStyles.SIDEBAR) as left_drawer:
                with ui.column().classes("w-full h-full no-wrap"):
                    with ui.column().classes("w-full flex-grow overflow-y-auto"):
                        if plugin_items:
                            ui.label("Services").classes(UIStyles.NAV_CATEGORY)
                            with ui.column().classes("w-full gap-1"):
                                for item in plugin_items:
                                    is_active = page_title == item["label"]
                                    style = (
                                        UIStyles.NAV_LINK_ACTIVE
                                        if is_active
                                        else UIStyles.NAV_LINK_INACTIVE
                                    )
                                    with ui.link(target=item["target"]).classes(
                                        f"{UIStyles.NAV_LINK_BASE} {style}"
                                    ):
                                        ui.icon(item["icon"], size="20px")
                                        ui.label(item["label"]).classes(
                                            "text-sm ml-3 font-medium"
                                        )
                        else:
                            ui.label("No Plugins active").classes(
                                "text-xs text-slate-500 italic p-4"
                            )

                    with ui.column().classes("w-full mt-auto pb-4"):
                        ui.separator().classes("mb-4 bg-slate-200 dark:bg-zinc-800")
                        ui.label("System").classes(UIStyles.NAV_CATEGORY)
                        with ui.column().classes("w-full gap-1"):
                            for item in core_items:
                                is_active = page_title == item["label"]
                                style = (
                                    UIStyles.NAV_LINK_ACTIVE
                                    if is_active
                                    else UIStyles.NAV_LINK_INACTIVE
                                )
                                with ui.link(target=item["target"]).classes(
                                    f"{UIStyles.NAV_LINK_BASE} {style}"
                                ):
                                    ui.icon(item["icon"], size="20px")
                                    ui.label(item["label"]).classes(
                                        "text-sm ml-3 font-medium"
                                    )

            # --- CONTENT ---
            with ui.column().classes("p-4 md:p-8 w-full max-w-7xl mx-auto flex-grow"):
                if inspect.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                else:
                    return fn(*args, **kwargs)

        return wrapper

    return decorator
