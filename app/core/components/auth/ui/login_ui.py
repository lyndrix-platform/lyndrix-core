from nicegui import ui, app
from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from core.components.auth.logic.providers.registry import provider_registry

log = get_logger("UI:Login")

def render_login_page():
    ui.query('body').style('background-color: #09090b;')

    with ui.card().classes('absolute-center shadow-2xl p-8 rounded-3xl border border-zinc-800 bg-zinc-900 text-zinc-100 w-full max-w-md'):
        with ui.column().classes('items-center w-full gap-4'):
            ui.icon('account_circle', size='64px').classes('text-indigo-500 mb-2')
            ui.label('Lyndrix Login').classes('text-2xl font-bold tracking-tight')

            user_input = (
                ui.input('Benutzername')
                .props('dark outlined autofocus name=username autocomplete=username')
                .classes('w-full')
            )
            pass_input = (
                ui.input('Passwort', password=True, password_toggle_button=True)
                .props('dark outlined name=password autocomplete=current-password')
                .classes('w-full')
            )
            pass_input.on('keydown.enter', lambda _: try_login())

            async def try_login():
                result = await provider_registry.authenticate(
                    user_input.value.strip(), pass_input.value
                )
                if result:
                    app.storage.user.update({
                        'authenticated': True,
                        'username': result.username,
                        'full_name': result.full_name,
                        'roles': result.roles,
                        'email': result.email,
                        'auth_provider': result.provider,
                        'extra_permissions': result.extra_permissions,
                    })
                    ui.notify(f'Willkommen zurück, {result.full_name}!', type='positive')
                    ui.navigate.to('/dashboard')
                else:
                    ui.notify('Anmeldung fehlgeschlagen: Falscher User oder Passwort', type='negative')

            ui.button('Einloggen', on_click=try_login).classes('w-full py-4 bg-indigo-600 rounded-xl font-bold')

            # --- SSO / OIDC buttons ---
            sso_providers = provider_registry.get_sso_providers()
            if sso_providers:
                with ui.row().classes('w-full items-center gap-2 my-1'):
                    ui.separator().classes('flex-grow bg-zinc-700')
                    ui.label('oder').classes('text-xs text-zinc-500 uppercase tracking-widest shrink-0')
                    ui.separator().classes('flex-grow bg-zinc-700')

                for provider in sso_providers:
                    async def sso_login(p=provider):
                        url = await p.build_login_url()
                        if url:
                            ui.navigate.to(url, new_tab=False)
                        else:
                            ui.notify(
                                f'SSO-Anbieter "{p.display_name}" ist derzeit nicht erreichbar.',
                                type='warning',
                            )

                    ui.button(
                        f'Anmelden mit {provider.display_name}',
                        icon='login',
                        on_click=sso_login,
                    ).props('outline').classes('w-full rounded-xl font-bold border-zinc-600 text-zinc-200 hover:border-indigo-500 hover:text-indigo-400')

            with ui.row().classes('items-center gap-2 opacity-50 mt-2'):
                ui.label('Standard: admin / lyndrix').classes('text-[10px] uppercase tracking-widest')
