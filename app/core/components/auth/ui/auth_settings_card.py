"""
Auth provider settings card for the Settings UI.

Renders an interactive configuration form for all auth providers
(Provider Chain, LDAP, OIDC/Authentik) plus the local Groups manager.

Config priority (highest wins):
  ENV var  >  Vault (editable here)  >  Default

ENV-sourced values are displayed as read-only with a lock icon.
Vault and Default values can be edited and saved back to Vault.
Saving triggers an immediate in-process provider re-initialization.
"""
from nicegui import ui

from core.logger import get_logger
from ui.theme import UIStyles
from core.components.auth.logic.auth_config import (
    auth_config_service,
    PROVIDER_CHAIN_SPEC,
    LDAP_SPECS,
    OIDC_SPECS,
    FieldSpec,
)
from core.components.auth.ui.groups_card import render_groups_card

log = get_logger("UI:AuthSettings")

_SOURCE_STYLES = {
    "env":     ("ENV",      "bg-amber-500/15 text-amber-300 border border-amber-500/30"),
    "vault":   ("Vault",    "bg-sky-500/15 text-sky-300 border border-sky-500/30"),
    "default": ("Standard", "bg-zinc-700/40 text-zinc-400 border border-zinc-600/40"),
}


# ──────────────────────────────────────────────────────────────────────────────
# Field row helper
# ──────────────────────────────────────────────────────────────────────────────

def _source_badge(source: str) -> None:
    text, cls = _SOURCE_STYLES.get(source, ("?", ""))
    ui.label(text).classes(
        f'text-[9px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full shrink-0 {cls}'
    )


def _field_row(spec: FieldSpec, value: str, source: str, inputs: dict) -> None:
    """
    Render one config field row and register the input widget in `inputs`.
    Locked (env-sourced) fields are shown read-only and not added to `inputs`.
    """
    locked = source == "env"

    with ui.row().classes(
        'w-full items-start gap-3 py-3 border-b border-zinc-800/30 last:border-0'
    ):
        # ── Label / env-var column ────────────────────────────────────────
        with ui.column().classes('gap-0 min-w-[190px] w-[190px] shrink-0 pt-1'):
            ui.label(spec.label).classes('text-xs font-semibold text-zinc-200 leading-snug')
            ui.label(spec.env_var).classes('text-[10px] font-mono text-zinc-600 leading-snug mt-0.5')

        # ── Input column ──────────────────────────────────────────────────
        with ui.column().classes('flex-grow gap-0.5'):
            if spec.is_bool:
                bool_val = value.lower() in ('true', '1', 'yes')
                sw = ui.switch(value=bool_val).props('dense color=primary')
                if locked:
                    sw.props('disable')
                    sw.classes('opacity-50')
                else:
                    inputs[spec.vault_key] = ('bool', sw)
            else:
                # For sensitive locked fields: never expose the actual secret value
                if locked and spec.sensitive:
                    display = ""
                    placeholder = f"Gesetzt via {spec.env_var}"
                else:
                    display = value
                    placeholder = spec.label

                inp = ui.input(
                    value=display,
                    placeholder=placeholder,
                    password=spec.sensitive and not locked,
                ).props('outlined dark dense' + (' readonly' if locked else '')).classes(
                    'w-full' + (' opacity-50' if locked else '')
                )
                if not locked:
                    inputs[spec.vault_key] = ('str', inp)

            if spec.hint:
                ui.label(spec.hint).classes(
                    'text-[10px] text-zinc-600 leading-snug'
                    + (' mt-0.5' if not spec.is_bool else '')
                )

        # ── Source / lock column ──────────────────────────────────────────
        with ui.column().classes('items-center shrink-0 pt-1 gap-1'):
            _source_badge(source)
            if locked:
                ui.icon('lock', size='14px').classes('text-amber-600/60')


# ──────────────────────────────────────────────────────────────────────────────
# Main render function
# ──────────────────────────────────────────────────────────────────────────────

async def render_auth_settings_card() -> None:
    """Full auth configuration UI: provider status, chain, LDAP, OIDC."""
    from core.services import vault_instance
    from core.components.auth.logic.providers.registry import provider_registry
    from core.components.auth.logic.auth_service import auth_service

    vault_data = auth_config_service.load_vault_data()
    vault_ok = vault_instance.is_connected

    # ── Vault offline warning ─────────────────────────────────────────────────
    if not vault_ok:
        with ui.row().classes(
            'w-full items-start gap-3 p-4 mb-2 '
            'bg-amber-500/10 border border-amber-500/30 rounded-xl'
        ):
            ui.icon('warning_amber', size='20px').classes('text-amber-400 shrink-0 mt-0.5')
            with ui.column().classes('gap-0'):
                ui.label('Vault nicht verbunden').classes(
                    'text-sm font-bold text-amber-300'
                )
                ui.label(
                    'Änderungen können nicht gespeichert werden. '
                    'Konfiguriere die Provider über Umgebungsvariablen (.env).'
                ).classes('text-xs text-amber-500/80 mt-0.5')

    # ── Helpers ───────────────────────────────────────────────────────────────

    def collect_values(inputs: dict) -> dict:
        result = {}
        for key, (typ, widget) in inputs.items():
            if typ == 'bool':
                result[key] = 'true' if widget.value else 'false'
            else:
                result[key] = widget.value or ''
        return result

    def save_and_reinit(inputs: dict, section: str) -> None:
        data = collect_values(inputs)
        try:
            auth_config_service.save_vault_data(data)
        except Exception as e:
            ui.notify(f'Fehler beim Speichern: {e}', type='negative')
            return
        try:
            auth_service.reinitialize_providers()
        except Exception as e:
            ui.notify(f'Gespeichert, aber Neuinitialisierung schlug fehl: {e}', type='warning')
            return
        ui.notify(f'{section} gespeichert — Provider neu geladen.', type='positive')

    with ui.column().classes('w-full gap-4'):

        # ── Provider Status Overview ──────────────────────────────────────────
        with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
            ui.element('div').classes(
                'h-1 w-full bg-gradient-to-r from-indigo-400 via-violet-400 to-purple-400'
            )
            with ui.column().classes('w-full flex-grow p-5 gap-3'):
                with ui.row().classes('w-full items-center justify-between'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('security', size='18px').classes('text-indigo-400')
                        ui.label('Aktive Provider').classes(UIStyles.TITLE_H3)

                    def do_reinit():
                        try:
                            auth_service.reinitialize_providers()
                            ui.notify('Provider neu geladen.', type='positive')
                        except Exception as e:
                            ui.notify(f'Fehler: {e}', type='negative')

                    ui.button('Neu laden', icon='refresh', on_click=do_reinit).props(
                        'outline size=sm color=indigo'
                    )

                ui.label(
                    'Beim Login werden die Provider in dieser Reihenfolge versucht. '
                    '"Übernehmen" speichert und lädt die Provider sofort neu.'
                ).classes(UIStyles.TEXT_MUTED + ' text-xs')

                all_providers = provider_registry.get_all()
                if not all_providers:
                    ui.label('Keine Provider registriert.').classes(
                        'text-xs text-zinc-600 italic mt-1'
                    )
                else:
                    with ui.column().classes('w-full gap-0 mt-1'):
                        for idx, p in enumerate(all_providers):
                            ok = p.is_configured()
                            s_cls = 'text-emerald-400' if ok else 'text-amber-400'
                            with ui.row().classes(
                                'w-full items-center gap-3 py-2 '
                                'border-b border-zinc-800/30 last:border-0'
                            ):
                                ui.label(str(idx + 1)).classes(
                                    'text-xs font-mono text-zinc-600 w-4 text-center shrink-0'
                                )
                                ui.icon('circle', size='8px').classes(s_cls + ' shrink-0')
                                ui.label(p.provider_id).classes(
                                    'text-sm font-mono font-bold text-zinc-200'
                                )
                                ui.label(p.display_name).classes('text-xs text-zinc-500 flex-grow')
                                if p.is_sso():
                                    ui.label('SSO').classes(
                                        'text-[9px] font-bold px-2 py-0.5 rounded-full '
                                        'bg-violet-500/15 text-violet-300 border border-violet-500/30'
                                    )
                                badge_ok = (
                                    'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                                    if ok else
                                    'bg-amber-500/15 text-amber-300 border border-amber-500/30'
                                )
                                ui.label('OK' if ok else 'Unvollständig').classes(
                                    f'text-[9px] font-bold px-2 py-0.5 rounded-full {badge_ok}'
                                )

        # ── Provider Chain ────────────────────────────────────────────────────
        chain_inputs: dict = {}
        with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
            ui.element('div').classes('h-1 w-full bg-gradient-to-r from-zinc-500 to-zinc-600')
            with ui.column().classes('w-full flex-grow p-5 gap-2'):
                with ui.row().classes('items-center gap-2 mb-1'):
                    ui.icon('sort', size='18px').classes('text-zinc-400')
                    ui.label('Provider-Reihenfolge').classes(UIStyles.TITLE_H3)

                val, src = auth_config_service.get_effective(PROVIDER_CHAIN_SPEC, vault_data)
                _field_row(PROVIDER_CHAIN_SPEC, val, src, chain_inputs)

                with ui.row().classes('w-full justify-end mt-3'):
                    ui.button(
                        'Übernehmen', icon='check',
                        on_click=lambda: save_and_reinit(chain_inputs, 'Provider Chain'),
                    ).props('unelevated size=sm color=primary').set_enabled(vault_ok)

        # ── LDAP / Active Directory ───────────────────────────────────────────
        ldap_inputs: dict = {}
        with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
            ui.element('div').classes('h-1 w-full bg-gradient-to-r from-sky-400 to-cyan-400')
            with ui.column().classes('w-full flex-grow p-5 gap-2'):

                with ui.row().classes('items-center gap-2 mb-1'):
                    ui.icon('folder_shared', size='18px').classes('text-sky-400')
                    ui.label('LDAP / Active Directory').classes(UIStyles.TITLE_H3)

                for spec in LDAP_SPECS:
                    val, src = auth_config_service.get_effective(spec, vault_data)
                    _field_row(spec, val, src, ldap_inputs)

                # ── Action row ────────────────────────────────────────────────
                ldap_status = ui.label('').classes('text-xs font-mono flex-grow text-right')

                async def test_ldap():
                    from core.components.auth.logic.providers.ldap import LDAPProvider
                    # Build a temporary provider from the current form values (or
                    # effective config) — no need for ldap to be in the active chain.
                    current_vd = auth_config_service.load_vault_data()
                    form_vals = collect_values(ldap_inputs)
                    # Overlay form values on top of vault data for the test
                    merged = {**current_vd, **{k: v for k, v in form_vals.items() if v}}
                    kwargs = auth_config_service.build_ldap_kwargs(merged)
                    if not kwargs.get("url") or not kwargs.get("bind_dn") or not kwargs.get("bind_password"):
                        ldap_status.classes(
                            add='text-amber-400', remove='text-emerald-400 text-red-400 text-zinc-400'
                        )
                        ldap_status.set_text('URL, Bind DN und Passwort müssen ausgefüllt sein.')
                        return
                    tmp = LDAPProvider(**kwargs)
                    ldap_status.classes(
                        add='text-zinc-400', remove='text-emerald-400 text-red-400 text-amber-400'
                    )
                    ldap_status.set_text('Verbinde…')
                    ok, msg = await tmp.test_connection()
                    ldap_status.classes(
                        add='text-emerald-400' if ok else 'text-red-400',
                        remove='text-zinc-400 text-amber-400 '
                               + ('text-red-400' if ok else 'text-emerald-400'),
                    )
                    ldap_status.set_text(msg)

                with ui.row().classes('w-full items-center gap-3 mt-3'):
                    ui.button(
                        'Verbindung testen', icon='cable', on_click=test_ldap
                    ).props('outline size=sm color=sky')
                    ldap_status  # positioned in the row via flex-grow
                    ui.button(
                        'Übernehmen', icon='check',
                        on_click=lambda: save_and_reinit(ldap_inputs, 'LDAP'),
                    ).props('unelevated size=sm color=primary').set_enabled(vault_ok)

        # ── OIDC / OAuth2 / Authentik ─────────────────────────────────────────
        oidc_inputs: dict = {}
        with ui.card().classes(UIStyles.CARD_GLASS + ' w-full').style('padding: 0; flex-wrap: nowrap'):
            ui.element('div').classes(
                'h-1 w-full bg-gradient-to-r from-violet-400 via-purple-400 to-pink-400'
            )
            with ui.column().classes('w-full flex-grow p-5 gap-2'):

                with ui.row().classes('items-center gap-2 mb-1'):
                    ui.icon('login', size='18px').classes('text-violet-400')
                    ui.label('OIDC / OAuth2 (Authentik, Keycloak, …)').classes(UIStyles.TITLE_H3)

                for spec in OIDC_SPECS:
                    val, src = auth_config_service.get_effective(spec, vault_data)
                    _field_row(spec, val, src, oidc_inputs)

                # ── Authentik quickstart reference ────────────────────────────
                with ui.expansion('Authentik Schnellstart', icon='help_outline').classes(
                    'w-full text-zinc-500 mt-2'
                ):
                    with ui.column().classes('gap-1 p-3'):
                        ui.label(
                            'In Authentik: Application → Provider → OAuth2/OpenID → '
                            'Redirect URI eintragen → Client ID / Secret kopieren.'
                        ).classes('text-xs text-zinc-500 mb-2')
                        for var, example in [
                            ('LYNDRIX_AUTH_PROVIDERS',   'local,oidc'),
                            ('LYNDRIX_OIDC_ISSUER',      'https://<authentik>/application/o/<slug>'),
                            ('LYNDRIX_OIDC_CLIENT_ID',   '<client-id aus Authentik>'),
                            ('LYNDRIX_OIDC_CLIENT_SECRET','<secret — bevorzugt in Vault speichern>'),
                            ('LYNDRIX_OIDC_REDIRECT_URI', 'https://<lyndrix-host>/auth/callback/oidc'),
                            ('LYNDRIX_OIDC_DISPLAY_NAME', 'Authentik'),
                            ('LYNDRIX_OIDC_ADMIN_GROUPS', 'lyndrix-admins'),
                        ]:
                            with ui.row().classes('items-start gap-3'):
                                ui.label(var).classes(
                                    'text-[10px] font-mono text-sky-400 shrink-0 w-64 pt-0.5'
                                )
                                ui.label(example).classes(
                                    'text-[10px] font-mono text-zinc-500 break-all'
                                )

                # ── Action row ────────────────────────────────────────────────
                oidc_status = ui.label('').classes('text-xs font-mono flex-grow text-right')

                async def test_oidc():
                    from core.components.auth.logic.providers.oidc import OIDCProvider
                    # Build a temporary provider from the current form values.
                    current_vd = auth_config_service.load_vault_data()
                    form_vals = collect_values(oidc_inputs)
                    merged = {**current_vd, **{k: v for k, v in form_vals.items() if v}}
                    kwargs = auth_config_service.build_oidc_kwargs(merged)
                    if not kwargs.get("issuer"):
                        oidc_status.classes(
                            add='text-amber-400', remove='text-emerald-400 text-red-400 text-zinc-400'
                        )
                        oidc_status.set_text('Issuer URL muss ausgefüllt sein.')
                        return
                    tmp = OIDCProvider(**kwargs)
                    oidc_status.classes(
                        add='text-zinc-400', remove='text-emerald-400 text-red-400 text-amber-400'
                    )
                    oidc_status.set_text('Lade Discovery…')
                    ok, msg = await tmp.probe_discovery()
                    oidc_status.classes(
                        add='text-emerald-400' if ok else 'text-red-400',
                        remove='text-zinc-400 text-amber-400 '
                               + ('text-red-400' if ok else 'text-emerald-400'),
                    )
                    oidc_status.set_text(msg)

                with ui.row().classes('w-full items-center gap-3 mt-3'):
                    ui.button(
                        'Discovery testen', icon='wifi_tethering', on_click=test_oidc
                    ).props('outline size=sm color=deep-purple')
                    oidc_status  # flex-grow positions it between buttons
                    ui.button(
                        'Übernehmen', icon='check',
                        on_click=lambda: save_and_reinit(oidc_inputs, 'OIDC'),
                    ).props('unelevated size=sm color=primary').set_enabled(vault_ok)

        # ── Local Groups & Permissions ────────────────────────────────────────
        await render_groups_card()
