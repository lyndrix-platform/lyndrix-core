"""NiceGUI card for the Notifications tab in core Settings.

Two sections:
  1. Registered Providers — shows every GatewayAdapter currently registered
     in the MessagingGateway, its configuration status, and the env vars
     needed to configure it.
  2. Endpoint Routing — lists every plugin-declared notification endpoint,
     lets the operator toggle active/inactive and assign a provider binding.
     Fields locked by env vars are rendered read-only with an amber lock badge.
"""
from __future__ import annotations

import os
from typing import Any

from nicegui import ui

from core.api import UIStyles
from core.i18n import t

from ..logic.endpoint_registry import endpoint_registry
from ..logic.precedence import (
    GLOBAL_DEFAULT_PROVIDER_ENV,
    env_is_locked_active,
    env_is_locked_provider,
    resolve_endpoint_state,
)
from ..logic.router_service import notification_router
from core.components.messaging.gateway import messaging_gateway


_DEFAULT_VALUE = "__default__"


def _collect_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plugin_id, ep in endpoint_registry.iter_all():
        state = resolve_endpoint_state(plugin_id, ep.name)
        rows.append({
            "plugin_id": plugin_id,
            "endpoint_name": ep.name,
            "description": ep.description,
            "declared": ep,
            "state": state,
            "active_locked": env_is_locked_active(plugin_id, ep.name),
            "provider_locked": env_is_locked_provider(plugin_id, ep.name),
        })
    rows.sort(key=lambda r: (r["plugin_id"], r["endpoint_name"]))
    return rows


def _plugin_display(plugin_id: str) -> str:
    try:
        from core.components.plugins.logic.manager import module_manager
        entry = module_manager.registry.get(plugin_id)
        if entry and entry.get("manifest"):
            return f"{entry['manifest'].name}  ·  {plugin_id}"
    except Exception:
        pass
    return plugin_id


def _provider_options() -> dict[str, str]:
    """Return select options as {value: label}.

    ``__default__`` is a reserved sentinel that clears the DB binding so the
    precedence resolver falls back to declaration default (which, for endpoints
    with ``external_default=True``, in turn resolves to the global default env).
    """
    options: dict[str, str] = {
        _DEFAULT_VALUE: t("notification_router.default_provider"),
    }
    for adapter in messaging_gateway.list_adapters():
        options[adapter.provider_id] = f"{adapter.display_name}  ·  {adapter.provider_id}"
    return options


def _provider_value_for_state(provider: str | None, provider_source: str) -> str:
    if provider_source in ("default", "global_default", "none") or provider is None:
        return _DEFAULT_VALUE
    return provider


def _render_endpoint_row(
    row: dict[str, Any],
    provider_options: dict[str, str],
) -> tuple:
    """Render one endpoint row and return (active_switch, provider_select) so the
    plugin-level Save button can read their current values.
    """
    ep = row["declared"]
    state = row["state"]
    active_locked = row["active_locked"]
    provider_locked = row["provider_locked"]

    with ui.column().classes("w-full gap-1 py-3 border-b border-slate-200 dark:border-white/10 last:border-0"):
        with ui.row().classes("w-full items-center justify-between gap-4"):
            with ui.column().classes("gap-0 flex-grow min-w-0"):
                ui.label(ep.name).classes("text-sm font-bold text-slate-800 dark:text-zinc-200 font-mono")
                if ep.description:
                    ui.label(ep.description).classes(UIStyles.TEXT_HINT)
                hints = []
                if ep.internal_toast:
                    hints.append(t("notification_router.internal_toast"))
                if ep.internal_persist:
                    hints.append(t("notification_router.internal_persist"))
                if ep.external_default:
                    hints.append(t("notification_router.external_default"))
                if hints:
                    ui.label("  ·  ".join(hints)).classes(
                        UIStyles.LABEL_MINI
                    )

            with ui.row().classes("items-center gap-3 shrink-0"):
                active_switch = ui.switch(value=bool(state.active))
                if active_locked:
                    active_switch.props("disable").classes("opacity-60")

                opts = dict(provider_options)
                current_pv = _provider_value_for_state(state.provider, state.provider_source)
                if current_pv not in opts:
                    opts[current_pv] = f"{current_pv}  ·  (not registered)"
                provider_select = ui.select(
                    options=opts,
                    value=current_pv,
                ).props("dense options-dense outlined").classes("w-72")
                if provider_locked:
                    provider_select.props("disable").classes("opacity-60")

        if active_locked or provider_locked:
            with ui.row().classes("items-center gap-3 mt-1"):
                if active_locked:
                    with ui.row().classes("items-center gap-1 text-amber-400"):
                        ui.icon("lock", size="12px")
                        ui.label(t("notification_router.locked_by_env")).classes("text-[11px]")
                if provider_locked:
                    with ui.row().classes("items-center gap-1 text-amber-400"):
                        ui.icon("lock", size="12px")
                        ui.label(t("notification_router.locked_by_env")).classes("text-[11px]")

    return active_switch, provider_select, active_locked, provider_locked


def _render_providers_card() -> None:
    """Show every registered GatewayAdapter with its config status and env-var hints."""
    adapters = messaging_gateway.list_adapters()
    gateway_providers_env = os.getenv("LYNDRIX_GATEWAY_PROVIDERS", "")
    global_default_env = os.getenv(GLOBAL_DEFAULT_PROVIDER_ENV)

    with ui.card().classes(UIStyles.CARD_GLASS + " w-full").style("padding: 0; flex-wrap: nowrap"):
        ui.element("div").classes(
            "h-1 w-full bg-gradient-to-r from-sky-400 via-cyan-400 to-teal-400"
        )
        with ui.column().classes("w-full p-5 gap-3"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("hub", size="18px").classes("text-cyan-400")
                ui.label(t("notification_router.providers_title")).classes(UIStyles.TITLE_H3)
            ui.label(t("notification_router.providers_subtitle")).classes(
                UIStyles.TEXT_MUTED + " text-xs"
            )

            # LYNDRIX_GATEWAY_PROVIDERS env-var status
            with ui.row().classes("w-full items-center gap-2 py-2 border-b border-slate-200 dark:border-white/10"):
                ui.label("LYNDRIX_GATEWAY_PROVIDERS").classes(
                    UIStyles.TEXT_HINT + " font-mono shrink-0"
                )
                if gateway_providers_env:
                    ui.label(gateway_providers_env).classes(
                        "text-xs font-mono text-emerald-300 truncate"
                    )
                    with ui.row().classes("items-center gap-1 text-amber-400 shrink-0"):
                        ui.icon("lock", size="11px")
                        ui.label("env").classes("text-[10px]")
                else:
                    ui.label(t("notification_router.providers_single_mode")).classes(
                        UIStyles.TEXT_HINT + " italic"
                    )

            if not adapters:
                with ui.row().classes("items-center gap-2 py-3 " + UIStyles.TEXT_MUTED):
                    ui.icon("warning_amber", size="16px").classes("text-amber-400")
                    ui.label(t("notification_router.providers_none")).classes("text-sm")
                # Hint how to get a provider registered
                ui.label(t("notification_router.providers_hint")).classes(
                    UIStyles.TEXT_HINT
                )
                return

            for adapter in adapters:
                _render_provider_row(adapter)

            # Global default provider footer
            ui.separator().classes("bg-slate-200 dark:bg-white/10 my-1")
            with ui.row().classes("w-full items-center justify-between gap-4"):
                with ui.column().classes("gap-0"):
                    ui.label("LYNDRIX_NOTIF_DEFAULT_PROVIDER").classes(
                        UIStyles.TEXT_HINT + " font-mono"
                    )
                    ui.label(t("notification_router.providers_default_hint")).classes(
                        UIStyles.TEXT_HINT
                    )
                if global_default_env:
                    with ui.row().classes("items-center gap-1"):
                        ui.label(global_default_env).classes("text-xs font-mono text-emerald-300")
                        with ui.row().classes("items-center gap-1 text-amber-400"):
                            ui.icon("lock", size="11px")
                            ui.label("env").classes("text-[10px]")
                else:
                    ui.label(t("notification_router.no_provider")).classes(
                        UIStyles.TEXT_HINT + " italic"
                    )


def _render_provider_row(adapter) -> None:
    """Single row inside the Providers card for one registered adapter."""
    provider_id: str = adapter.provider_id
    config_fields = adapter.get_config_fields()

    with ui.column().classes("w-full gap-2 py-3 border-b border-slate-200 dark:border-white/10 last:border-0"):
        # Header: status dot + name + provider_id pill
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("circle", size="10px").classes("text-emerald-400 shrink-0")
            ui.label(adapter.display_name).classes("text-sm font-bold text-slate-800 dark:text-zinc-200")
            ui.label(provider_id).classes(
                UIStyles.TEXT_HINT
                + " font-mono px-2 py-0.5 rounded-full bg-slate-200 dark:bg-white/10 border border-slate-200 dark:border-white/10"
            )

        if not config_fields:
            # Adapter doesn't expose config fields — show generic env-var hint
            ui.label(t("notification_router.provider_no_config")).classes(
                UIStyles.TEXT_HINT + " italic ml-5"
            )
            return

        # Per-field: show status + editable input when not env-locked
        input_widgets: dict[str, any] = {}

        for cf in config_fields:
            with ui.row().classes("w-full items-center gap-3 ml-5 flex-wrap"):
                with ui.column().classes("gap-0 min-w-0 flex-grow"):
                    ui.label(cf.label).classes("text-xs font-bold text-slate-700 dark:text-zinc-300")
                    ui.label(cf.env_var).classes(UIStyles.TEXT_HINT + " font-mono")

                if cf.is_env_locked:
                    masked = "●●●●●●●●" if cf.sensitive and cf.current_value else (cf.current_value or "—")
                    with ui.row().classes("items-center gap-2 shrink-0"):
                        ui.label(masked).classes(UIStyles.TEXT_HINT + " font-mono")
                        with ui.row().classes("items-center gap-1 text-amber-400"):
                            ui.icon("lock", size="11px")
                            ui.label(t("notification_router.locked_by_env")).classes("text-[10px]")
                else:
                    widget = ui.input(
                        value=cf.current_value or "",
                        password=cf.sensitive,
                        placeholder=cf.placeholder or "",
                    ).props("outlined dark dense").classes("w-64")
                    input_widgets[cf.key] = widget

        # Save button reads .value at click time — no fragile event binding needed.
        editable_fields = [cf for cf in config_fields if not cf.is_env_locked]
        if editable_fields:
            def _save_provider(
                _adapter=adapter,
                _fields=editable_fields,
                _widgets=input_widgets,
            ) -> None:
                all_ok = True
                for cf in _fields:
                    w = _widgets.get(cf.key)
                    val = w.value if w else ""
                    if not _adapter.save_config(cf.key, val):
                        all_ok = False
                if all_ok:
                    ui.notify(
                        f"{_adapter.display_name}: {t('notification_router.provider_saved')}",
                        type="positive",
                    )
                else:
                    ui.notify(
                        f"{_adapter.display_name}: {t('notification_router.provider_save_failed')}",
                        type="negative",
                    )

            with ui.row().classes("justify-end ml-5"):
                ui.button(
                    t("notification_router.save"), icon="save", on_click=_save_provider
                ).props("unelevated size=sm color=primary")


def render_notifications_settings_card() -> None:
    rows = _collect_rows()
    provider_options = _provider_options()
    global_default_env = os.getenv(GLOBAL_DEFAULT_PROVIDER_ENV)

    with ui.column().classes("w-full gap-4"):
        # ── Header ──────────────────────────────────────────────────────────
        with ui.card().classes(UIStyles.CARD_GLASS + " w-full").style("padding: 0; flex-wrap: nowrap"):
            ui.element("div").classes(
                "h-1 w-full bg-gradient-to-r from-pink-400 via-fuchsia-400 to-violet-400"
            )
            with ui.column().classes("w-full p-5 gap-2"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("campaign", size="18px").classes("text-fuchsia-400")
                    ui.label(t("notification_router.title")).classes(UIStyles.TITLE_H3)
                ui.label(t("notification_router.subtitle")).classes(
                    UIStyles.TEXT_MUTED + " text-xs"
                )

        # ── Registered Providers ─────────────────────────────────────────────
        _render_providers_card()

        # ── Endpoint Routing ─────────────────────────────────────────────────
        if not rows:
            with ui.card().classes(UIStyles.CARD_GLASS + " w-full p-5"):
                ui.label(t("notification_router.empty")).classes(UIStyles.TEXT_MUTED)
            return

        # Group by plugin
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["plugin_id"], []).append(row)

        for plugin_id, plugin_rows in grouped.items():
            with ui.card().classes(UIStyles.CARD_GLASS + " w-full").style("padding: 0; flex-wrap: nowrap"):
                ui.element("div").classes(
                    "h-1 w-full bg-gradient-to-r from-emerald-400 via-sky-400 to-indigo-500"
                )
                with ui.column().classes("w-full p-5 gap-1"):
                    with ui.row().classes("items-center gap-2 mb-1"):
                        ui.icon("extension", size="18px").classes("text-indigo-400")
                        ui.label(_plugin_display(plugin_id)).classes(UIStyles.TITLE_H3)

                    # Collect widget refs so the Save button can read current values.
                    row_widgets: list[tuple] = []
                    for row in plugin_rows:
                        widgets = _render_endpoint_row(row, provider_options)
                        row_widgets.append((row, *widgets))

                    # Save button — reads .value from each widget at click time,
                    # same pattern as _render_editable_settings_card in settings_ui.py.
                    async def _save(
                        rw: list = row_widgets,
                        pid: str = plugin_id,
                    ) -> None:
                        errors: list[str] = []
                        for r, act_sw, prov_sel, act_locked, prov_locked in rw:
                            ep_name = r["endpoint_name"]
                            kwargs: dict = {}
                            if not act_locked:
                                kwargs["active"] = bool(act_sw.value)
                            if not prov_locked:
                                raw = prov_sel.value
                                kwargs["provider"] = (
                                    None if raw == _DEFAULT_VALUE else raw
                                )
                            if not kwargs:
                                continue
                            try:
                                await notification_router.update_binding(
                                    pid, ep_name, **kwargs
                                )
                            except Exception as exc:
                                errors.append(f"{ep_name}: {exc}")

                        if errors:
                            ui.notify(
                                t("notification_router.save_failed",
                                  error="; ".join(errors)),
                                type="negative",
                            )
                        else:
                            ui.notify(t("notification_router.saved"), type="positive")

                    with ui.row().classes("w-full justify-end mt-3"):
                        ui.button(
                            t("notification_router.save"),
                            icon="save",
                            on_click=_save,
                        ).props("unelevated size=sm color=primary")
