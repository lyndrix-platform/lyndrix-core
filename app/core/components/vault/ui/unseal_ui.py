import time
import asyncio

from nicegui import ui

from core.bus import bus
from core.i18n import t
from ui.theme import UIStyles, apply_theme

# Rate limiting state
_unseal_attempts = []
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 60


def render_unseal_page():
    """Renders a minimal, isolated unseal mask with rate limiting."""

    apply_theme(page_title="Unseal")
    ui.query("body").classes(add=UIStyles.AUTH_PAGE_BG)

    with ui.card().classes(UIStyles.AUTH_CARD):
        with ui.column().classes("items-center w-full gap-4"):
            ui.icon("lock", size="48px").classes(UIStyles.AUTH_HERO_ICON)
            ui.label(t("auth.unseal.title")).classes(UIStyles.AUTH_TITLE)
            ui.label(t("auth.unseal.subtitle")).classes(UIStyles.AUTH_SUBTITLE)

            master_key = (
                ui.input(t("auth.unseal.master_key"))
                .props(f"{UIStyles.AUTH_INPUT_PROPS} password autofocus")
                .classes("w-full mb-2")
            )

            status_label = ui.label("").classes(UIStyles.AUTH_STATUS_PENDING)

            async def attempt_unseal():
                global _unseal_attempts
                now = time.time()

                # Purge old attempts outside the lockout window
                _unseal_attempts = [ts for ts in _unseal_attempts if now - ts < _LOCKOUT_SECONDS]

                if len(_unseal_attempts) >= _MAX_ATTEMPTS:
                    remaining = int(_LOCKOUT_SECONDS - (now - _unseal_attempts[0]))
                    status_label.set_text(t("auth.unseal.locked_for", seconds=remaining))
                    status_label.classes(
                        add=UIStyles.AUTH_STATUS_ERROR,
                        remove=UIStyles.AUTH_STATUS_PENDING,
                    )
                    return

                if not master_key.value:
                    ui.notify(t("auth.unseal.empty_key"), type="warning")
                    return

                _unseal_attempts.append(now)
                status_label.set_text(t("auth.unseal.decrypting"))
                status_label.classes(
                    add=UIStyles.AUTH_STATUS_PENDING,
                    remove=UIStyles.AUTH_STATUS_ERROR,
                )

                bus.emit("vault:unseal_requested", {"key": master_key.value})

            master_key.on("keydown.enter", attempt_unseal)

            ui.button(t("auth.unseal.submit"), on_click=attempt_unseal).classes(
                UIStyles.AUTH_BUTTON_PRIMARY
            ).props("unelevated")

            with ui.row().classes("items-center gap-2 opacity-30 mt-4"):
                ui.element("div").classes("w-2 h-2 rounded-full bg-emerald-500")
                ui.label(t("auth.unseal.bus_active")).classes(UIStyles.AUTH_HINT_TEXT)

    # Poll vault connection status in the UI context
    async def check_status():
        from core.services import vault_instance
        if vault_instance.is_connected:
            status_timer.cancel()
            ui.notify(t("auth.unseal.unlocked"), type="positive")
            await asyncio.sleep(0.5)
            ui.navigate.to("/")

    status_timer = ui.timer(0.5, check_status)

    @bus.subscribe("vault:unseal_failed")
    def on_vault_failed(payload):
        status_label.set_text(t("auth.unseal.incorrect_key"))
        status_label.classes(
            add=UIStyles.AUTH_STATUS_ERROR,
            remove=UIStyles.AUTH_STATUS_PENDING,
        )
