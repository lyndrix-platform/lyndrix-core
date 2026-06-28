import json

from nicegui import ui

from config import settings
from core.theming import get_theme_engine
from version import __version__

class UIStyles:
    # ----------------------------------------------------
    # 1. KARTEN & CONTAINER
    # ----------------------------------------------------
    CARD_BASE = 'p-6 rounded-2xl shadow-lg border border-slate-200 dark:border-white/5 overflow-hidden lyndrix-card'
    CARD_GLASS = 'p-6 rounded-2xl shadow-lg border border-slate-200 dark:border-white/5 overflow-hidden lyndrix-glass-card'
    CARD_HIGHLIGHT = 'p-6 rounded-2xl border-2 border-primary bg-sky-50/50 dark:bg-cyan-500/5'
    CARD_COMPACT = 'p-4 rounded-xl border border-slate-200 dark:border-white/5 lyndrix-card'
    MODAL_CONTAINER = '!bg-slate-50 dark:!bg-[#0f1629] border border-slate-200 dark:border-white/5 shadow-2xl rounded-2xl'
    PANEL_SUBTLE = 'border border-slate-200 dark:border-white/5 bg-slate-50 dark:bg-white/[0.03]'

    # ----------------------------------------------------
    # 1b. SELECTABLE TILES (border+bg+text per state; layout/size at call site)
    # ----------------------------------------------------
    TILE_BASE = 'rounded-lg border transition-colors select-none'
    TILE_SELECTED = 'border-primary bg-primary/10 text-primary'
    TILE_DEFAULT = 'border-slate-300 dark:border-white/10 hover:border-primary/50 bg-slate-50 dark:bg-white/5 hover:bg-slate-100 dark:hover:bg-white/10 text-slate-700 dark:text-zinc-200 cursor-pointer'
    TILE_DISABLED = 'border-slate-200 dark:border-white/5 bg-slate-100 dark:bg-white/[0.02] opacity-40 cursor-not-allowed text-slate-400 dark:text-zinc-600'
    TILE_WARNING = 'border-amber-500/60 hover:border-amber-400 bg-amber-500/10 text-amber-300 cursor-pointer'

    # ----------------------------------------------------
    # 2. STRUKTUR & LAYOUT
    # ----------------------------------------------------
    HEADER = '!bg-white/80 dark:!bg-[#0a0e1a]/80 backdrop-blur-md border-b border-slate-200 dark:border-white/5 text-slate-800 dark:text-white'
    TAB_BAR = 'w-full justify-start border-b border-slate-200 dark:border-white/5 text-slate-500 dark:text-zinc-400'
    # ----------------------------------------------------
    # 3. TYPOGRAFIE
    # ----------------------------------------------------
    TITLE_H1 = 'text-3xl font-bold tracking-tight text-slate-900 dark:text-white'
    TITLE_H2 = 'text-2xl font-bold tracking-tight text-slate-800 dark:text-zinc-100'
    TITLE_H3 = 'text-lg font-bold text-slate-800 dark:text-zinc-100'
    TEXT_MUTED = 'text-sm text-slate-600 dark:text-zinc-200'
    TEXT_HINT = 'text-xs text-slate-600 dark:text-zinc-300'
    LABEL_HEADING = 'text-sm font-semibold text-slate-800 dark:text-zinc-200'
    LABEL_FIELD = 'text-xs font-semibold text-slate-600 dark:text-zinc-300'
    LABEL_MINI = 'text-[10px] font-bold uppercase tracking-widest text-slate-500 dark:text-zinc-400'

    # ----------------------------------------------------
    # 4. BUTTONS
    # ----------------------------------------------------
    BUTTON_PRIMARY = 'w-full py-4 rounded-xl font-bold transition-all text-white lyndrix-btn-primary'
    BUTTON_SECONDARY = 'w-full py-4 bg-slate-200 dark:bg-white/5 hover:bg-slate-300 dark:hover:bg-white/10 rounded-xl font-bold transition-all text-slate-900 dark:text-white'

    # Standard input props string (pass via .props(f"{UIStyles.INPUT_PROPS} ..."))
    INPUT_PROPS = 'outlined dense'

    # ----------------------------------------------------
    # 5. DROPDOWN MENÜS
    # ----------------------------------------------------
    MENU_CONTAINER = 'lyndrix-menu shadow-2xl rounded-2xl overflow-hidden border border-slate-200 dark:border-white/5'
    MENU_ITEM = 'text-slate-700 dark:text-zinc-200 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors px-4 py-2'
    MENU_ITEM_DANGER = 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors px-4 py-2'

    # ----------------------------------------------------
    # 6. SIDEBAR & NAVIGATION
    # ----------------------------------------------------
    SIDEBAR = '!bg-slate-50 dark:!bg-[#0a0e1a] border-r border-slate-200 dark:border-white/5 !p-4 flex flex-col transition-colors'
    NAV_CATEGORY = 'px-3 mb-2 mt-4 text-[11px] font-bold text-slate-400 dark:text-zinc-500 uppercase tracking-widest'
    NAV_LINK_BASE = 'w-full flex items-center px-3 py-2 no-underline transition-all'
    NAV_LINK_ACTIVE = 'bg-sky-50 dark:bg-cyan-500/10 text-primary border-l-2 border-primary rounded-r-xl'
    NAV_LINK_INACTIVE = 'text-slate-500 dark:text-zinc-400 hover:bg-slate-200 dark:hover:bg-white/5 rounded-xl'

    # ----------------------------------------------------
    # 7. AUTH SURFACES (login, unseal, profile)
    # ----------------------------------------------------
    AUTH_PAGE_BG = 'lyndrix-auth-page'
    AUTH_CARD = 'absolute-center shadow-2xl p-8 rounded-3xl border border-white/5 text-zinc-100 w-full max-w-md lyndrix-auth-card'
    AUTH_HERO_ICON = 'text-primary mb-2'
    AUTH_TITLE = 'text-2xl font-bold tracking-tight text-zinc-100'
    AUTH_SUBTITLE = 'text-center text-sm text-zinc-400 mb-4'
    AUTH_INPUT_PROPS = 'dark outlined color=primary'
    AUTH_BUTTON_PRIMARY = 'w-full py-4 rounded-xl font-bold transition-all text-white lyndrix-btn-primary'
    AUTH_BUTTON_SSO = 'w-full rounded-xl font-bold lyndrix-btn-sso'
    AUTH_DIVIDER_LINE = 'flex-grow bg-white/10'
    AUTH_DIVIDER_LABEL = 'text-xs text-zinc-500 uppercase tracking-widest shrink-0'
    AUTH_HINT_TEXT = 'text-[10px] uppercase tracking-widest text-zinc-500'
    AUTH_STATUS_PENDING = 'text-xs font-mono text-cyan-300'
    AUTH_STATUS_ERROR = 'text-xs font-mono text-red-400'

    # ----------------------------------------------------
    # 8. PROFILE / CARD CHROME (auth_cards, groups_card, ...)
    # ----------------------------------------------------
    PROFILE_CARD = 'p-0 rounded-2xl shadow-lg border border-white/5 w-full lyndrix-glass-card'
    GRAD_BAR_ACCENT = 'h-1 w-full bg-gradient-to-r from-cyan-400 via-blue-400 to-violet-400'
    GRAD_BAR_SUCCESS = 'h-1 w-full bg-gradient-to-r from-emerald-400 to-teal-400'
    GRAD_BAR_INFO = 'h-1 w-full bg-gradient-to-r from-sky-400 to-cyan-400'
    GRAD_BAR_NEUTRAL = 'h-1 w-full bg-white/5'

    BADGE_NEUTRAL = 'text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-white/5 text-zinc-300 border border-white/10'
    BADGE_ACCENT = 'text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-cyan-500/15 text-cyan-300 border border-cyan-500/30'
    BADGE_ACCENT_VIOLET = 'text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-violet-500/15 text-violet-300 border border-violet-500/30'
    BADGE_SUCCESS = 'text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
    BADGE_WARNING = 'text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30'
    BADGE_DANGER = 'text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-red-500/15 text-red-300 border border-red-500/30'

    CHIP_ROLE = 'text-[9px] font-mono px-2 py-0.5 rounded-full bg-white/5 text-zinc-400 border border-white/10'
    CHIP_OVERFLOW = 'text-[9px] text-zinc-500'
    CHIP_PERMISSION = 'text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'

    WARNING_BANNER = 'w-full items-start gap-2 p-3 mb-1 bg-amber-500/10 border border-amber-500/30 rounded-xl'
    WARNING_TEXT = 'text-xs text-amber-400/80'

    ICON_PRIMARY = 'text-primary'
    ICON_MUTED = 'text-zinc-500'
    ICON_SUCCESS = 'text-emerald-400'
    ICON_INFO = 'text-cyan-400'
    ICON_WARNING = 'text-amber-400 shrink-0 mt-0.5'

    STATUS_TEXT_SUCCESS = 'text-xs text-emerald-400'
    STATUS_TEXT_ERROR = 'text-xs text-red-400'
    STATUS_TEXT_WARNING = 'text-xs text-amber-400'
    STATUS_TEXT_NEUTRAL = 'text-xs text-zinc-500'


def _full_title(page_title: str | None) -> str:
    if not page_title:
        return settings.APP_TITLE
    return f"{page_title} | {settings.APP_TITLE}"


def _metadata_script(title: str) -> str:
    safe_title = json.dumps(title)
    asset_version = json.dumps(__version__)
    return f"""
        (() => {{
            const title = {safe_title};
            const version = {asset_version};
            document.title = title;

            const ensureLink = (id, rel, href, sizes=null, type=null) => {{
                let el = document.getElementById(id);
                if (!el) {{
                    el = document.createElement('link');
                    el.id = id;
                    document.head.appendChild(el);
                }}
                el.rel = rel;
                el.href = href;
                if (sizes) el.sizes = sizes;
                else el.removeAttribute('sizes');
                if (type) el.type = type;
                else el.removeAttribute('type');
            }};

            ensureLink('lyndrix-favicon-32', 'icon', `/assets/icons/favicon-32x32.png?v=${{version}}`, '32x32', 'image/png');
            ensureLink('lyndrix-favicon-16', 'icon', `/assets/icons/favicon-16x16.png?v=${{version}}`, '16x16', 'image/png');
            ensureLink('lyndrix-favicon-ico', 'icon', `/favicon.ico?v=${{version}}`);
            ensureLink('lyndrix-apple-touch', 'apple-touch-icon', `/assets/icons/apple-touch-icon.png?v=${{version}}`, '180x180', 'image/png');
            ensureLink('lyndrix-manifest', 'manifest', `/site.webmanifest?v=${{version}}`);
        }})();
    """


def _apply_style_overrides(theme_pref: str, body_bg: str, body_fg: str, active_theme: str):
    # NiceGUI is dark-first: the --lx-* vars are consumed mostly under `.dark`,
    # and most callers use theme_pref='auto', so keep the dark palette as the
    # base unless the user explicitly selected light. When the theme engine is
    # enabled the values come from tokens.json (single source of truth); when
    # disabled we fall back to the previous hardcoded literals.
    use_dark = theme_pref != 'light'
    if settings.THEME_ENGINE_ENABLED:
        _lx_vars = get_theme_engine().resolve_css_variables(use_dark, active_theme)
    else:
        _lx_vars = {
            '--lx-bg': '#0a0e1a',
            '--lx-surface': '#0f1629',
            '--lx-elevated': '#131c33',
            '--lx-border': 'rgba(0, 212, 255, 0.15)',
            '--lx-border-soft': 'rgba(255, 255, 255, 0.06)',
            '--lx-text': '#f0f6ff',
            '--lx-text-muted': '#8b95b5',
            '--lx-accent': '#00d4ff',
            '--lx-accent-2': '#0ea5e9',
            '--lx-accent-3': '#8b5cf6',
            '--lx-radius-sm': '6px',
            '--lx-radius-md': '12px',
            '--lx-radius-lg': '20px',
            '--lx-glow': '0 0 24px rgba(0, 212, 255, 0.25)',
            '--lx-state-up': '#10b981',
            '--lx-state-down': '#f43f5e',
            '--lx-state-paused': '#f59e0b',
            '--lx-state-unknown': '#0ea5e9',
        }
    _lx_var_lines = '\n'.join(f'                {k}: {v};' for k, v in _lx_vars.items())
    ui.add_head_html(f'''
        <script>
            (function() {{
                var pref = '{theme_pref}';
                if (pref === 'auto') {{
                    pref = localStorage.getItem('theme_pref') || 'auto';
                }}
                var isDark = pref === 'dark' || (pref === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);

                if (isDark) {{
                    document.documentElement.classList.add('dark');
                }}
            }})();
        </script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap');

            @font-face {{
                font-family: 'Lyndrix System Sans';
                src:
                    local('JetBrainsMonoNL Nerd Font Propo'),
                    local('JetBrainsMono Nerd Font Propo'),
                    local('JetBrainsMonoNL Nerd Font'),
                    local('JetBrains Mono');
                font-display: swap;
            }}

            @font-face {{
                font-family: 'Lyndrix System Mono';
                src:
                    local('JetBrainsMonoNL Nerd Font Mono'),
                    local('JetBrainsMono Nerd Font Mono'),
                    local('JetBrainsMonoNL Nerd Font'),
                    local('JetBrains Mono');
                font-display: swap;
            }}

            :root {{
{_lx_var_lines}
                --lx-font-sans: 'Lyndrix System Sans', 'JetBrainsMonoNL Nerd Font Propo', 'JetBrainsMono Nerd Font Propo', 'JetBrains Mono', 'Noto Sans', system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                --lx-font-mono: 'Lyndrix System Mono', 'JetBrainsMonoNL Nerd Font Mono', 'JetBrainsMono Nerd Font Mono', 'JetBrains Mono', 'Fira Code', 'Cascadia Code', Consolas, monospace;
                --q-font-family: var(--lx-font-sans);
            }}

            html,
            body {{
                font-family: var(--lx-font-sans) !important;
                transition: background-color 0.3s ease;
                -webkit-font-smoothing: antialiased;
            }}
            /* Force app typography across Quasar/NiceGUI controls. */
            body,
            .q-layout,
            .q-page-container,
            .q-page,
            .q-card,
            .q-btn,
            .q-item,
            .q-tab,
            .q-chip,
            .q-menu,
            .q-tooltip,
            .q-dialog,
            .q-field__native,
            .q-field__input,
            .q-table,
            .q-list,
            .q-notification,
            input,
            textarea,
            select,
            button,
            label,
            p {{
                font-family: var(--lx-font-sans) !important;
            }}
            body.body--dark, html.dark body {{
                background-color: var(--lx-bg) !important;
                color: var(--lx-text) !important;
            }}
            body.body--dark .q-layout,
            body.body--dark .q-page-container,
            body.body--dark .q-page {{
                background-color: var(--lx-bg) !important;
            }}
            body.body--light {{
                background-color: #f8fafc !important;
                color: #0f172a !important;
            }}
            code, .font-mono, .lyndrix-mono {{
                font-family: var(--lx-font-mono);
            }}
            code,
            pre,
            kbd,
            samp,
            .font-mono,
            .lyndrix-mono,
            .q-editor__content pre,
            .q-editor__content code {{
                font-family: var(--lx-font-mono) !important;
            }}

            .lyndrix-card {{
                background-color: white !important;
            }}
            .body--dark .lyndrix-card, .dark .lyndrix-card {{
                background-color: var(--lx-surface) !important;
                border-color: var(--lx-border-soft) !important;
            }}

            .lyndrix-glass-card {{
                backdrop-filter: blur(16px) saturate(140%);
                -webkit-backdrop-filter: blur(16px) saturate(140%);
            }}
            .body--light .lyndrix-glass-card {{
                background-color: rgba(255, 255, 255, 0.72) !important;
            }}
            .body--dark .lyndrix-glass-card, .dark .lyndrix-glass-card {{
                background: linear-gradient(180deg, rgba(19, 28, 51, 0.7), rgba(15, 22, 41, 0.55)) !important;
                border-color: var(--lx-border-soft) !important;
            }}

            /* ── Auth surfaces ──────────────────────────────────── */
            .lyndrix-auth-page {{
                background: radial-gradient(circle at 20% 20%, rgba(0, 212, 255, 0.10), transparent 55%),
                            radial-gradient(circle at 80% 80%, rgba(139, 92, 246, 0.10), transparent 55%),
                            var(--lx-bg) !important;
                min-height: 100vh;
            }}
            .lyndrix-auth-card {{
                background: linear-gradient(180deg, rgba(19, 28, 51, 0.85), rgba(15, 22, 41, 0.78)) !important;
                backdrop-filter: blur(20px) saturate(150%);
                -webkit-backdrop-filter: blur(20px) saturate(150%);
                box-shadow: 0 30px 80px -20px rgba(0, 0, 0, 0.6), 0 0 40px rgba(0, 212, 255, 0.08);
            }}

            /* ── Buttons ─────────────────────────────────────────── */
            .lyndrix-btn-primary {{
                background: linear-gradient(135deg, var(--lx-accent) 0%, var(--lx-accent-2) 60%, var(--lx-accent-3) 100%) !important;
                box-shadow: 0 10px 24px -10px rgba(0, 212, 255, 0.55);
            }}
            .lyndrix-btn-primary:hover {{
                filter: brightness(1.08);
                box-shadow: 0 14px 32px -10px rgba(0, 212, 255, 0.7);
            }}
            .lyndrix-btn-sso {{
                background: rgba(255, 255, 255, 0.03) !important;
                border: 1px solid rgba(255, 255, 255, 0.10) !important;
                color: #e2e8f0 !important;
                transition: all 0.2s ease;
            }}
            .lyndrix-btn-sso:hover {{
                border-color: var(--lx-accent) !important;
                color: var(--lx-accent) !important;
                box-shadow: 0 0 18px rgba(0, 212, 255, 0.2);
            }}

            /* Card style: no top gradient stripes. */
            .q-card > [class*="h-1"][class*="w-full"][class*="bg-gradient-to-r"] {{
                display: none !important;
            }}

            .q-menu.lyndrix-menu {{
                background-color: white !important;
                min-width: 200px;
            }}
            .body--dark .q-menu.lyndrix-menu, .dark .q-menu.lyndrix-menu {{
                background-color: var(--lx-elevated) !important;
                color: var(--lx-text) !important;
            }}

            .body--dark .q-card, .dark .q-card {{
                background: var(--lx-surface);
                color: var(--lx-text);
            }}

            .body--dark .bg-white, .dark .bg-white {{
                background-color: var(--lx-surface) !important;
                color: var(--lx-text) !important;
            }}

            /* ── Notification popup — centered on small screens ────────────── */
            @media (max-width: 639px) {{
                .q-menu.lyndrix-notif-popup {{
                    left: 50% !important;
                    right: auto !important;
                    transform: translateX(-50%) !important;
                }}
            }}

            /* State colours (--lx-state-*) are emitted in the :root block above,
               token-driven via the theme engine. */

            ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
            ::-webkit-scrollbar-track {{ background: transparent; }}
            body.body--dark ::-webkit-scrollbar-thumb, html.dark ::-webkit-scrollbar-thumb {{
                background: rgba(0, 212, 255, 0.25);
                border-radius: 4px;
            }}
            body.body--light ::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 4px; }}
        </style>
    ''')


def _hydrate_ui_styles(theme_id: str):
    if not settings.THEME_ENGINE_ENABLED:
        return

    styles = get_theme_engine().resolve_component_styles(theme_id)
    for key, value in styles.items():
        if hasattr(UIStyles, key):
            setattr(UIStyles, key, value)


def set_page_metadata(page_title: str | None):
    ui.run_javascript(_metadata_script(_full_title(page_title)))


def apply_theme(theme_pref: str = 'auto', page_title: str | None = None, theme_id: str | None = None):
    """Apply current visual theme and browser metadata."""
    is_dark = theme_pref == 'dark'
    active_theme = theme_id or settings.DEFAULT_THEME_ID

    if settings.THEME_ENGINE_ENABLED:
        palette = get_theme_engine().resolve_runtime_palette(is_dark, active_theme)
    else:
        palette = {
            'primary': '#00d4ff',
            'secondary': '#0ea5e9',
            'accent': '#8b5cf6',
            'positive': '#10b981',
            'negative': '#ef4444',
            'info': '#00d4ff',
            'warning': '#f59e0b',
        }

    _hydrate_ui_styles(active_theme)
    ui.colors(**palette)

    if settings.THEME_ENGINE_ENABLED:
        body_bg = get_theme_engine().resolve_color('bg_body', is_dark, active_theme) or '#0a0e1a'
        body_fg = get_theme_engine().resolve_color('text_body', is_dark, active_theme) or '#f0f6ff'
    else:
        body_bg = '#0a0e1a'
        body_fg = '#f0f6ff'

    full_title = _full_title(page_title)
    asset_version = __version__
    ui.add_head_html(f'''
        <title id="lyndrix-title">{full_title}</title>
        <link id="lyndrix-favicon-32" rel="icon" type="image/png" sizes="32x32" href="/assets/icons/favicon-32x32.png?v={asset_version}" />
        <link id="lyndrix-favicon-16" rel="icon" type="image/png" sizes="16x16" href="/assets/icons/favicon-16x16.png?v={asset_version}" />
        <link id="lyndrix-favicon-ico" rel="icon" href="/favicon.ico?v={asset_version}" />
        <link id="lyndrix-apple-touch" rel="apple-touch-icon" sizes="180x180" href="/assets/icons/apple-touch-icon.png?v={asset_version}" />
        <link id="lyndrix-manifest" rel="manifest" href="/site.webmanifest?v={asset_version}" />
        <meta name="theme-color" content="{body_bg}" />
        <script>{_metadata_script(full_title)}</script>
    ''')

    _apply_style_overrides(theme_pref, body_bg, body_fg, active_theme)