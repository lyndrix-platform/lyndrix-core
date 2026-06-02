from nicegui import ui

from ui.theme import UIStyles
from core.logger import get_logger
from core.i18n import t
from core.session import clear_user_session, get_user_storage

log = get_logger("UI:AuthCards")


def render_user_settings_card():
    session = get_user_storage() or {}
    username = str(session.get("username", ""))
    full_name = str(session.get("full_name", "") or t("auth.user_card.default_user"))
    email = str(session.get("email", "—"))
    provider = str(session.get("auth_provider", "local"))
    roles = list(session.get("roles", []))
    extra_perms = list(session.get("extra_permissions", []))

    initials = "".join(p[0].upper() for p in full_name.split()[:2]) or "U"
    is_local = provider == "local"

    with ui.column().classes("w-full gap-4"):

        # ── Profile banner ───────────────────────────────────────────────────
        with ui.card().classes(UIStyles.PROFILE_CARD).style(
            "padding: 0; flex-wrap: nowrap"
        ):
            ui.element("div").classes(UIStyles.GRAD_BAR_ACCENT)
            with ui.row().classes("w-full flex-grow items-center gap-5 p-5"):
                ui.avatar(initials, color="primary", text_color="white").classes(
                    "text-2xl font-black shrink-0"
                )
                with ui.column().classes("gap-1 flex-grow"):
                    ui.label(full_name).classes("text-xl font-bold text-zinc-100")
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"@{username}").classes(
                            "text-sm font-mono text-zinc-400"
                        )
                        if email and email != "—":
                            ui.label("·").classes("text-zinc-600")
                            ui.label(email).classes("text-sm text-zinc-500")

                with ui.column().classes("items-end gap-1 shrink-0"):
                    # Provider badge
                    badge_cls = (
                        UIStyles.BADGE_ACCENT if is_local else UIStyles.BADGE_ACCENT_VIOLET
                    )
                    ui.label(provider).classes(badge_cls)
                    # Roles / groups chips
                    if roles:
                        with ui.row().classes("flex-wrap gap-1 justify-end"):
                            for role in roles[:6]:  # cap display at 6
                                ui.label(role).classes(UIStyles.CHIP_ROLE)
                            if len(roles) > 6:
                                ui.label(f"+{len(roles) - 6}").classes(
                                    UIStyles.CHIP_OVERFLOW
                                )

        # ── Password change ──────────────────────────────────────────────────
        if is_local:
            _render_password_change_card(username)
        else:
            with ui.card().classes(UIStyles.PROFILE_CARD).style(
                "padding: 0; flex-wrap: nowrap"
            ):
                ui.element("div").classes(UIStyles.GRAD_BAR_NEUTRAL)
                with ui.row().classes("w-full items-center gap-3 p-5"):
                    ui.icon("info", size="18px").classes(f"{UIStyles.ICON_MUTED} shrink-0")
                    ui.label(
                        t(
                            "auth.user_card.password_via_provider",
                            provider=provider,
                        )
                    ).classes("text-sm text-zinc-500")

        # ── API keys ─────────────────────────────────────────────────────────
        if username:
            _render_api_keys_card(username)

        # ── Direct permissions ───────────────────────────────────────────────
        if extra_perms:
            with ui.card().classes(UIStyles.PROFILE_CARD).style(
                "padding: 0; flex-wrap: nowrap"
            ):
                ui.element("div").classes(UIStyles.GRAD_BAR_SUCCESS)
                with ui.column().classes("w-full flex-grow p-5 gap-2"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("verified_user", size="18px").classes(UIStyles.ICON_SUCCESS)
                        ui.label(t("auth.user_card.direct_permissions")).classes(
                            UIStyles.TITLE_H3
                        )
                    with ui.row().classes("flex-wrap gap-1 mt-1"):
                        for perm in extra_perms:
                            ui.label(perm).classes(UIStyles.CHIP_PERMISSION)

        # ── Logout ───────────────────────────────────────────────────────────
        with ui.row().classes("w-full justify-end"):

            async def logout():
                clear_user_session()
                ui.navigate.to("/login")

            ui.button(t("auth.user_card.logout"), icon="logout", on_click=logout).props(
                "outline color=negative size=sm"
            )


def _render_password_change_card(username: str) -> None:
    from core.components.auth.logic.user_service import user_service

    with ui.card().classes(UIStyles.PROFILE_CARD).style(
        "padding: 0; flex-wrap: nowrap"
    ):
        ui.element("div").classes(UIStyles.GRAD_BAR_INFO)
        with ui.column().classes("w-full flex-grow p-5 gap-3"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("lock_reset", size="18px").classes(UIStyles.ICON_INFO)
                ui.label(t("auth.password.title")).classes(
                    "text-base font-bold text-zinc-100"
                )

            warn = user_service.env_override_warning(username)
            if warn:
                with ui.row().classes(UIStyles.WARNING_BANNER):
                    ui.icon("warning_amber", size="16px").classes(UIStyles.ICON_WARNING)
                    ui.label(warn).classes(UIStyles.WARNING_TEXT)

            cur_pw = (
                ui.input(
                    t("auth.password.current"), password=True, password_toggle_button=True
                )
                .props(f"{UIStyles.AUTH_INPUT_PROPS} dense autocomplete=current-password")
                .classes("w-full max-w-sm")
            )
            new_pw = (
                ui.input(
                    t("auth.password.new"), password=True, password_toggle_button=True
                )
                .props(f"{UIStyles.AUTH_INPUT_PROPS} dense autocomplete=new-password")
                .classes("w-full max-w-sm")
            )
            conf_pw = (
                ui.input(
                    t("auth.password.confirm"),
                    password=True,
                    password_toggle_button=True,
                )
                .props(f"{UIStyles.AUTH_INPUT_PROPS} dense autocomplete=new-password")
                .classes("w-full max-w-sm")
            )

            status_label = ui.label("").classes("text-xs")

            def do_change():
                if new_pw.value != conf_pw.value:
                    status_label.classes(
                        add="text-red-400", remove="text-emerald-400 text-zinc-500"
                    )
                    status_label.set_text(t("auth.password.mismatch"))
                    return
                ok, msg = user_service.change_password(
                    username, cur_pw.value, new_pw.value
                )
                if ok:
                    status_label.classes(
                        add="text-emerald-400", remove="text-red-400 text-zinc-500"
                    )
                    cur_pw.set_value("")
                    new_pw.set_value("")
                    conf_pw.set_value("")
                else:
                    status_label.classes(
                        add="text-red-400", remove="text-emerald-400 text-zinc-500"
                    )
                status_label.set_text(msg)

            ui.button(t("auth.password.submit"), icon="save", on_click=do_change).props(
                "unelevated size=sm color=primary"
            )


def _render_api_keys_card(username: str) -> None:
    """Render the per-user API key management card (create / list / revoke)."""
    from core.components.auth.logic.api_key_service import api_key_service

    with ui.card().classes(UIStyles.PROFILE_CARD).style(
        "padding: 0; flex-wrap: nowrap"
    ):
        ui.element("div").classes(UIStyles.GRAD_BAR_ACCENT)
        with ui.column().classes("w-full flex-grow p-5 gap-3"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("vpn_key", size="18px").classes("text-amber-400")
                ui.label("API Keys").classes("text-base font-bold text-zinc-100")
            ui.label(
                "Personal API keys authenticate as you and inherit your permissions. "
                "Send the key in the 'X-API-Key' header or as 'Authorization: Bearer <key>'. "
                "The key is shown only once."
            ).classes("text-xs text-zinc-500")

            list_container = ui.column().classes("w-full gap-1")

            def _format_dt(value) -> str:
                if not value:
                    return "—"
                try:
                    return value.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    return str(value)

            def _refresh_keys():
                list_container.clear()
                keys = api_key_service.list_for_user(username)
                with list_container:
                    if not keys:
                        ui.label("No API keys yet.").classes(
                            "text-xs text-zinc-600 italic py-1"
                        )
                    for info in keys:
                        with ui.row().classes(
                            "w-full items-center gap-3 py-2 "
                            "border-b border-zinc-800/40 last:border-0"
                        ):
                            with ui.column().classes("gap-0 flex-grow min-w-0"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(info.label).classes(
                                        "text-sm font-bold text-zinc-200 truncate"
                                    )
                                    if info.revoked:
                                        ui.label("revoked").classes(
                                            "text-[10px] uppercase tracking-wider "
                                            "text-red-400 border border-red-500/30 rounded px-1"
                                        )
                                    for sc in info.scopes:
                                        ui.label(sc).classes(
                                            "text-[10px] font-mono text-amber-300 "
                                            "border border-amber-500/30 rounded px-1"
                                        )
                                    if not info.scopes:
                                        ui.label("inherits permissions").classes(
                                            "text-[10px] text-zinc-600"
                                        )
                                ui.label(
                                    f"{info.prefix}…  ·  created {_format_dt(info.created_at)}"
                                    f"  ·  last used {_format_dt(info.last_used_at)}"
                                ).classes("text-[11px] font-mono text-zinc-500 truncate")
                            if not info.revoked:
                                ui.button(
                                    icon="delete",
                                    on_click=lambda _, k=info.id: _revoke(k),
                                ).props("flat round dense color=negative").tooltip("Revoke")

            def _revoke(key_id: int):
                api_key_service.revoke(key_id, username)
                ui.notify("API key revoked.", type="warning")
                _refresh_keys()

            ui.separator().classes("my-1 bg-zinc-800/40")

            # ── Create new key ────────────────────────────────────────────────
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                label_input = ui.input("Key label", value="API Key").props(
                    "outlined dark dense"
                ).classes("max-w-xs")
                read_cb = ui.checkbox("api:read", value=True)
                write_cb = ui.checkbox("api:write", value=False)

            def _create():
                scopes = []
                if read_cb.value:
                    scopes.append("api:read")
                if write_cb.value:
                    scopes.append("api:write")
                raw, _info = api_key_service.create(
                    username, label_input.value or "API Key", scopes
                )
                label_input.set_value("API Key")
                _refresh_keys()
                _show_key_dialog(raw)

            ui.button("Generate API key", icon="add", on_click=_create).props(
                "unelevated size=sm color=primary"
            )

            _refresh_keys()


def _show_key_dialog(raw_key: str) -> None:
    """Show the freshly created key exactly once, with a copy button."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg gap-3 p-5"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("vpn_key", size="20px").classes("text-amber-400")
            ui.label("Your new API key").classes("text-base font-bold text-zinc-100")
        ui.label(
            "Copy it now — it will not be shown again."
        ).classes("text-xs text-red-400")
        key_box = ui.input(value=raw_key).props("outlined dark readonly").classes("w-full")  # noqa: F841  (rendered widget; reference kept for clarity)
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(
                "Copy",
                icon="content_copy",
                on_click=lambda: (
                    ui.run_javascript(
                        f"navigator.clipboard.writeText({raw_key!r})"
                    ),
                    ui.notify("Copied to clipboard.", type="positive"),
                ),
            ).props("outline size=sm color=primary")
            ui.button("Done", on_click=dialog.close).props("unelevated size=sm color=primary")
    dialog.open()
