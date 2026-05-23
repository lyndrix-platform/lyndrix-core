from nicegui import ui
from core.logger import get_logger
from core.i18n import t
from core.components.auth.logic.providers.registry import provider_registry
from core.session import update_user_session
from ui.theme import UIStyles, apply_theme

log = get_logger("UI:Login")


def render_login_page():
    apply_theme(page_title="Login")
    ui.query("body").classes(add=UIStyles.AUTH_PAGE_BG)

    with ui.card().classes(UIStyles.AUTH_CARD):
        with ui.column().classes("items-center w-full gap-4"):
            ui.icon("account_circle", size="64px").classes(UIStyles.AUTH_HERO_ICON)
            ui.label(t("auth.login.title")).classes(UIStyles.AUTH_TITLE)

            user_input = (
                ui.input(t("auth.login.username"))
                .props(f"{UIStyles.AUTH_INPUT_PROPS} autofocus name=username autocomplete=username")
                .classes("w-full")
            )
            pass_input = (
                ui.input(t("auth.login.password"), password=True, password_toggle_button=True)
                .props(f"{UIStyles.AUTH_INPUT_PROPS} name=password autocomplete=current-password")
                .classes("w-full")
            )
            pass_input.on("keydown.enter", lambda _: try_login())

            async def try_login():
                username = user_input.value.strip()
                if not username or not pass_input.value:
                    ui.notify(
                        t("auth.login.empty_creds"), type="warning"
                    )
                    return

                result = await provider_registry.authenticate(
                    username, pass_input.value
                )
                if result:
                    update_user_session(
                        {
                            "authenticated": True,
                            "username": result.username,
                            "full_name": result.full_name,
                            "roles": result.roles,
                            "email": result.email,
                            "auth_provider": result.provider,
                            "extra_permissions": result.extra_permissions,
                        }
                    )
                    ui.notify(
                        t("auth.login.welcome", name=result.full_name),
                        type="positive",
                    )
                    ui.navigate.to("/dashboard")
                else:
                    pass_input.set_value("")
                    ui.notify(
                        t("auth.login.failed"),
                        type="negative",
                    )

            ui.button(t("auth.login.submit"), on_click=try_login).classes(
                UIStyles.AUTH_BUTTON_PRIMARY
            ).props("unelevated")

            # --- SSO / OIDC buttons ---
            sso_providers = provider_registry.get_sso_providers()
            if sso_providers:
                with ui.row().classes("w-full items-center gap-2 my-1"):
                    ui.separator().classes(UIStyles.AUTH_DIVIDER_LINE)
                    ui.label(t("auth.login.or")).classes(UIStyles.AUTH_DIVIDER_LABEL)
                    ui.separator().classes(UIStyles.AUTH_DIVIDER_LINE)

                for provider in sso_providers:

                    async def sso_login(p=provider):
                        url = await p.build_login_url()
                        if url:
                            ui.navigate.to(url, new_tab=False)
                        else:
                            ui.notify(
                                t(
                                    "auth.login.sso_unavailable",
                                    name=p.display_name,
                                ),
                                type="warning",
                            )

                    ui.button(
                        t("auth.login.sso_with", provider=provider.display_name),
                        icon="login",
                        on_click=sso_login,
                    ).props("outline").classes(UIStyles.AUTH_BUTTON_SSO)

            with ui.row().classes("items-center gap-2 opacity-50 mt-2"):
                ui.label(t("auth.login.default_creds_hint")).classes(UIStyles.AUTH_HINT_TEXT)
