import json

from nicegui import ui

from config import settings
from core.theming import get_theme_engine
from version import __version__

class UIStyles:
    # ----------------------------------------------------
    # 1. KARTEN & CONTAINER
    # ----------------------------------------------------
    CARD_BASE = 'p-[calc(var(--lx-space-unit)*6)] rounded-[var(--lx-radius-2xl)] shadow-lg border border-slate-200 dark:border-white/5 overflow-hidden lyndrix-card'
    CARD_GLASS = 'p-[calc(var(--lx-space-unit)*6)] rounded-[var(--lx-radius-2xl)] shadow-lg border border-slate-200 dark:border-white/5 overflow-hidden lyndrix-glass-card'
    CARD_HIGHLIGHT = 'p-[calc(var(--lx-space-unit)*6)] rounded-[var(--lx-radius-2xl)] border-2 border-primary bg-sky-50/50 dark:bg-cyan-500/5'
    CARD_COMPACT = 'p-[calc(var(--lx-space-unit)*4)] rounded-[var(--lx-radius-xl)] border border-slate-200 dark:border-white/5 lyndrix-card'
    MODAL_CONTAINER = '!bg-slate-50 dark:!bg-[#0f1629] border border-slate-200 dark:border-white/5 shadow-2xl rounded-[var(--lx-radius-2xl)]'
    PANEL_SUBTLE = 'border border-slate-200 dark:border-white/5 bg-slate-50 dark:bg-white/[0.03]'

    # ----------------------------------------------------
    # 1b. SELECTABLE TILES (border+bg+text per state; layout/size at call site)
    # ----------------------------------------------------
    TILE_BASE = 'rounded-[var(--lx-radius-lg)] border transition-colors select-none'
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
    BUTTON_PRIMARY = 'w-full py-[calc(var(--lx-space-unit)*4)] rounded-[var(--lx-radius-xl)] font-bold transition-all text-white lyndrix-btn-primary'
    BUTTON_SECONDARY = 'w-full py-[calc(var(--lx-space-unit)*4)] bg-slate-200 dark:bg-white/5 hover:bg-slate-300 dark:hover:bg-white/10 rounded-[var(--lx-radius-xl)] font-bold transition-all text-slate-900 dark:text-white'

    # Standard input props string (pass via .props(f"{UIStyles.INPUT_PROPS} ..."))
    INPUT_PROPS = 'outlined dense'

    # ----------------------------------------------------
    # 5. DROPDOWN MENÜS
    # ----------------------------------------------------
    MENU_CONTAINER = 'lyndrix-menu shadow-2xl rounded-[var(--lx-radius-2xl)] overflow-hidden border border-slate-200 dark:border-white/5'
    MENU_ITEM = 'text-slate-700 dark:text-zinc-200 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors px-[calc(var(--lx-space-unit)*4)] py-[calc(var(--lx-space-unit)*2)]'
    MENU_ITEM_DANGER = 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors px-[calc(var(--lx-space-unit)*4)] py-[calc(var(--lx-space-unit)*2)]'

    # ----------------------------------------------------
    # 6. SIDEBAR & NAVIGATION
    # ----------------------------------------------------
    SIDEBAR = '!bg-slate-50 dark:!bg-[#0a0e1a] border-r border-slate-200 dark:border-white/5 !p-[calc(var(--lx-space-unit)*4)] flex flex-col transition-colors'
    NAV_CATEGORY = 'px-[calc(var(--lx-space-unit)*3)] mb-2 mt-4 text-[11px] font-bold text-slate-400 dark:text-zinc-500 uppercase tracking-widest'
    NAV_LINK_BASE = 'w-full flex items-center px-[calc(var(--lx-space-unit)*3)] py-[calc(var(--lx-space-unit)*2)] no-underline transition-all'
    NAV_LINK_ACTIVE = 'bg-sky-50 dark:bg-cyan-500/10 text-primary border-l-2 border-primary rounded-r-[var(--lx-radius-xl)]'
    NAV_LINK_INACTIVE = 'text-slate-500 dark:text-zinc-400 hover:bg-slate-200 dark:hover:bg-white/5 rounded-[var(--lx-radius-xl)]'

    # ----------------------------------------------------
    # 7. AUTH SURFACES (login, unseal, profile)
    # ----------------------------------------------------
    AUTH_PAGE_BG = 'lyndrix-auth-page'
    AUTH_CARD = 'absolute-center shadow-2xl p-[calc(var(--lx-space-unit)*8)] rounded-[var(--lx-radius-3xl)] border border-white/5 text-zinc-100 w-full max-w-md lyndrix-auth-card'
    AUTH_HERO_ICON = 'text-primary mb-2'
    AUTH_TITLE = 'text-2xl font-bold tracking-tight text-zinc-100'
    AUTH_SUBTITLE = 'text-center text-sm text-zinc-400 mb-4'
    AUTH_INPUT_PROPS = 'dark outlined color=primary'
    AUTH_BUTTON_PRIMARY = 'w-full py-[calc(var(--lx-space-unit)*4)] rounded-[var(--lx-radius-xl)] font-bold transition-all text-white lyndrix-btn-primary'
    AUTH_BUTTON_SSO = 'w-full rounded-[var(--lx-radius-xl)] font-bold lyndrix-btn-sso'
    AUTH_DIVIDER_LINE = 'flex-grow bg-white/10'
    AUTH_DIVIDER_LABEL = 'text-xs text-zinc-500 uppercase tracking-widest shrink-0'
    AUTH_HINT_TEXT = 'text-[10px] uppercase tracking-widest text-zinc-500'
    AUTH_STATUS_PENDING = 'text-xs font-mono text-cyan-300'
    AUTH_STATUS_ERROR = 'text-xs font-mono text-red-400'

    # ----------------------------------------------------
    # 8. PROFILE / CARD CHROME (auth_cards, groups_card, ...)
    # ----------------------------------------------------
    PROFILE_CARD = 'p-0 rounded-[var(--lx-radius-2xl)] shadow-lg border border-white/5 w-full lyndrix-glass-card'
    GRAD_BAR_ACCENT = 'h-1 w-full lyndrix-grad-bar bg-[var(--lx-gradient-accent)]'
    GRAD_BAR_SUCCESS = 'h-1 w-full bg-gradient-to-r from-emerald-400 to-teal-400'
    GRAD_BAR_INFO = 'h-1 w-full bg-gradient-to-r from-sky-400 to-cyan-400'
    GRAD_BAR_NEUTRAL = 'h-1 w-full bg-white/5'

    BADGE_NEUTRAL = 'text-[10px] font-bold uppercase tracking-wider px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-white/5 text-zinc-300 border border-white/10'
    BADGE_ACCENT = 'text-[10px] font-bold uppercase tracking-wider px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-cyan-500/15 text-cyan-300 border border-cyan-500/30'
    BADGE_ACCENT_VIOLET = 'text-[10px] font-bold uppercase tracking-wider px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-violet-500/15 text-violet-300 border border-violet-500/30'
    BADGE_SUCCESS = 'text-[10px] font-bold uppercase tracking-wider px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
    BADGE_WARNING = 'text-[10px] font-bold uppercase tracking-wider px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-amber-500/15 text-amber-300 border border-amber-500/30'
    BADGE_DANGER = 'text-[10px] font-bold uppercase tracking-wider px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-red-500/15 text-red-300 border border-red-500/30'

    CHIP_ROLE = 'text-[9px] font-mono px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-white/5 text-zinc-400 border border-white/10'
    CHIP_OVERFLOW = 'text-[9px] text-zinc-500'
    CHIP_PERMISSION = 'text-[10px] font-mono px-[calc(var(--lx-space-unit)*2)] py-[calc(var(--lx-space-unit)*0.5)] rounded-[var(--lx-radius-full)] bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'

    WARNING_BANNER = 'w-full items-start gap-[calc(var(--lx-space-unit)*2)] p-[calc(var(--lx-space-unit)*3)] mb-1 bg-amber-500/10 border border-amber-500/30 rounded-[var(--lx-radius-xl)]'
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

    # ----------------------------------------------------
    # 9. SHARED COMPONENT CLASSES (Theming v2 T0b)
    # ----------------------------------------------------
    # Plain, token-driven CSS classes injected by _apply_style_overrides()'s
    # <style> block AND mirrored verbatim in lyndrix-ui/src/index.css, so the
    # NiceGUI and React stacks consume the IDENTICAL class off the same --lx-*
    # tokens (this is what stops the two GUIs from drifting). Apply them with
    # e.g. ``ui.element().classes(UIStyles.LOG_PANEL)``.
    LOG_PANEL = 'lx-log-panel'
    TERMINAL = 'lx-terminal'
    CARD_ACCENT = 'lx-card-accent'
    CARD_ACCENT_SUCCESS = 'lx-card-accent lx-card-accent--success'
    CARD_ACCENT_WARNING = 'lx-card-accent lx-card-accent--warning'
    CARD_ACCENT_INFO = 'lx-card-accent lx-card-accent--info'
    CARD_ACCENT_DANGER = 'lx-card-accent lx-card-accent--danger'
    MODAL_OVERLAY = 'lx-modal-overlay'
    MODAL_GLASS = 'lx-modal'
    STATUS_RAIL = 'lx-status-rail'
    STATUS_RAIL_UP = 'lx-status-rail lx-status-rail--up'
    STATUS_RAIL_DOWN = 'lx-status-rail lx-status-rail--down'
    STATUS_RAIL_PAUSED = 'lx-status-rail lx-status-rail--paused'
    STATUS_RAIL_SUCCESS = 'lx-status-rail lx-status-rail--success'
    ROW_HOVER = 'lx-row-hover'
    BADGE_STATE = 'lx-badge'
    BADGE_STATE_UP = 'lx-badge lx-badge--up'
    BADGE_STATE_DOWN = 'lx-badge lx-badge--down'
    BADGE_STATE_PAUSED = 'lx-badge lx-badge--paused'
    BADGE_STATE_UNKNOWN = 'lx-badge lx-badge--unknown'
    BADGE_STATE_SUCCESS = 'lx-badge lx-badge--success'
    STEPPER = 'lx-stepper'
    ENTITY_CARD = 'lx-entity-card'
    IFRAME_FRAME = 'lx-iframe-frame'


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
            # --- Theming v2 Phase-1 categories (kept identical to the
            # ThemeEngine contract defaults in core/theming/engine.py so the
            # engine-on and engine-off variable sets never diverge) ---
            '--lx-radius-sm': '0.125rem',
            '--lx-radius': '0.25rem',
            '--lx-radius-md': '0.375rem',
            '--lx-radius-lg': '0.5rem',
            '--lx-radius-xl': '0.75rem',
            '--lx-radius-2xl': '1rem',
            '--lx-radius-3xl': '1.5rem',
            '--lx-radius-full': '9999px',
            '--lx-space-unit': '0.25rem',
            '--lx-font-sans': "'Lyndrix System Sans', 'JetBrainsMonoNL Nerd Font Propo', 'JetBrainsMono Nerd Font Propo', 'JetBrains Mono', 'Noto Sans', system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
            '--lx-font-mono': "'Lyndrix System Mono', 'JetBrainsMonoNL Nerd Font Mono', 'JetBrainsMono Nerd Font Mono', 'JetBrains Mono', 'Fira Code', 'Cascadia Code', Consolas, monospace",
            '--lx-text-scale': '1',
            '--lx-shadow-sm': '0 1px 2px 0 rgb(0 0 0 / 0.25)',
            '--lx-shadow-md': '0 8px 24px -12px rgb(0 0 0 / 0.45)',
            '--lx-shadow-lg': '0 30px 80px -20px rgb(0 0 0 / 0.6)',
            '--lx-shadow-glow': '0 0 30px rgb(0 212 255 / 0.3)',
            '--lx-blur-xs': '10px',
            '--lx-blur-sm': '8px',
            '--lx-blur-md': '16px',
            '--lx-blur-lg': '24px',
            '--lx-transition-fast': '150ms',
            '--lx-transition-base': '250ms',
            '--lx-transition-slow': '400ms',
            '--lx-ease': 'cubic-bezier(0.4, 0, 0.2, 1)',
            '--lx-gradient-accent': 'linear-gradient(135deg, var(--lx-accent), var(--lx-accent-2), var(--lx-accent-3))',
            '--lx-gradient-success': 'linear-gradient(90deg, var(--lx-state-success), var(--lx-chart-8))',
            '--lx-gradient-warning': 'linear-gradient(90deg, var(--lx-warning), var(--lx-chart-4))',
            '--lx-gradient-info': 'linear-gradient(90deg, var(--lx-accent-2), var(--lx-accent))',
            '--lx-gradient-danger': 'linear-gradient(90deg, var(--lx-state-down), var(--lx-chart-5))',
            '--lx-border-width': '1px',
            '--lx-rail-width': '3px',
            '--lx-glow': '0 0 30px rgb(0 212 255 / 0.3)',
            '--lx-state-up': '#10b981',
            '--lx-state-down': '#f43f5e',
            '--lx-state-paused': '#f59e0b',
            '--lx-state-unknown': '#0ea5e9',
            # --- Theming v2 T0 additions (kept in lock-step with the
            # ThemeEngine dark-mode output so engine-on/off never diverge) ---
            '--lx-state-success': '#10b981',
            '--lx-state-marked': '#6366f1',
            '--lx-chart-1': '#8b5cf6',
            '--lx-chart-2': '#0ea5e9',
            '--lx-chart-3': '#10b981',
            '--lx-chart-4': '#f59e0b',
            '--lx-chart-5': '#f43f5e',
            '--lx-chart-6': '#6366f1',
            '--lx-chart-7': '#06b6d4',
            '--lx-chart-8': '#14b8a6',
            '--lx-terminal-bg': '#050807',
            '--lx-terminal-fg': '#4ade80',
            '--lx-terminal-accent': '#22d3ee',
            '--lx-icon-xs': '14px',
            '--lx-icon-sm': '16px',
            '--lx-icon-md': '18px',
            '--lx-icon-lg': '32px',
            '--lx-z-dropdown': '1000',
            '--lx-z-modal': '1100',
            '--lx-z-toast': '1200',
            '--lx-scrim': 'rgb(0 0 0 / 0.55)',
            '--lx-text-2xs': 'calc(0.6875rem * var(--lx-text-scale))',
            '--lx-text-3xs': 'calc(0.625rem * var(--lx-text-scale))',
            '--lx-glass-saturate': '160%',
            '--lx-on-accent-text': '#0a0e1a',
            '--lx-on-accent-text-light': '#ffffff',
            '--lx-badge-bg-opacity': '0.15',
            '--lx-badge-border-opacity': '0.30',
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
                --q-font-family: var(--lx-font-sans);
            }}

            html,
            body {{
                font-family: var(--lx-font-sans) !important;
                transition: background-color var(--lx-transition-base) var(--lx-ease);
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
                background-color: {body_bg} !important;
                color: {body_fg} !important;
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
                backdrop-filter: blur(var(--lx-blur-md)) saturate(140%);
                -webkit-backdrop-filter: blur(var(--lx-blur-md)) saturate(140%);
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
                background: radial-gradient(circle at 20% 20%, color-mix(in srgb, var(--lx-accent) 10%, transparent), transparent 55%),
                            radial-gradient(circle at 80% 80%, color-mix(in srgb, var(--lx-accent-3) 10%, transparent), transparent 55%),
                            var(--lx-bg) !important;
                min-height: 100vh;
            }}
            .lyndrix-auth-card {{
                background: linear-gradient(180deg, rgba(19, 28, 51, 0.85), rgba(15, 22, 41, 0.78)) !important;
                backdrop-filter: blur(var(--lx-blur-lg)) saturate(150%);
                -webkit-backdrop-filter: blur(var(--lx-blur-lg)) saturate(150%);
                box-shadow: var(--lx-shadow-lg), 0 0 40px color-mix(in srgb, var(--lx-accent) 8%, transparent);
            }}

            /* ── Buttons ─────────────────────────────────────────── */
            .lyndrix-btn-primary {{
                background: var(--lx-gradient-accent) !important;
                box-shadow: 0 10px 24px -10px color-mix(in srgb, var(--lx-accent) 55%, transparent);
            }}
            .lyndrix-btn-primary:hover {{
                filter: brightness(1.08);
                box-shadow: 0 14px 32px -10px color-mix(in srgb, var(--lx-accent) 70%, transparent);
            }}
            .lyndrix-btn-sso {{
                background: rgba(255, 255, 255, 0.03) !important;
                border: 1px solid rgba(255, 255, 255, 0.10) !important;
                color: #e2e8f0 !important;
                transition: all var(--lx-transition-fast) var(--lx-ease);
            }}
            .lyndrix-btn-sso:hover {{
                border-color: var(--lx-accent) !important;
                color: var(--lx-accent) !important;
                box-shadow: 0 0 18px color-mix(in srgb, var(--lx-accent) 20%, transparent);
            }}

            /* Card style: no top gradient stripes. */
            .q-card > [class*="h-1"][class*="w-full"][class*="bg-gradient-to-r"],
            .q-card > .lyndrix-grad-bar {{
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

            /* ── Shared component classes (Theming v2 T0b) ──────────────────
               Defined identically here (NiceGUI) and in lyndrix-ui/src/index.css
               (React) so BOTH stacks consume the same class names off the same
               --lx-* tokens. Keep the two copies in sync. */
            .lx-log-panel {{
                background: var(--lx-log-bg);
                color: var(--lx-log-fg);
                font-family: var(--lx-font-mono);
                font-size: var(--lx-text-2xs);
                line-height: 1.5;
                border: 1px solid var(--lx-border-soft);
                border-radius: var(--lx-radius-md);
                padding: calc(var(--lx-space-unit) * 3);
                overflow: auto;
                white-space: pre-wrap;
                word-break: break-word;
            }}
            .lx-log-panel .lx-log-accent {{ color: var(--lx-log-accent); }}
            .lx-terminal {{
                background: var(--lx-terminal-bg);
                color: var(--lx-terminal-fg);
                font-family: var(--lx-font-mono);
                font-size: var(--lx-text-2xs);
                line-height: 1.5;
                border: 1px solid color-mix(in srgb, var(--lx-terminal-fg) 22%, transparent);
                border-radius: var(--lx-radius-md);
                padding: calc(var(--lx-space-unit) * 3);
                overflow: auto;
                white-space: pre-wrap;
                word-break: break-word;
            }}
            .lx-terminal .lx-terminal-accent {{ color: var(--lx-terminal-accent); }}

            .lx-card-accent {{ position: relative; }}
            .lx-card-accent::before {{
                content: '';
                position: absolute;
                top: 0; left: 0; right: 0;
                height: var(--lx-rail-width);
                border-radius: var(--lx-radius-md) var(--lx-radius-md) 0 0;
                background: var(--lx-gradient-accent);
            }}
            .lx-card-accent--success::before {{ background: var(--lx-gradient-success); }}
            .lx-card-accent--warning::before {{ background: var(--lx-gradient-warning); }}
            .lx-card-accent--info::before {{ background: var(--lx-gradient-info); }}
            .lx-card-accent--danger::before {{ background: var(--lx-gradient-danger); }}

            .lx-modal-overlay {{
                position: fixed;
                inset: 0;
                z-index: var(--lx-z-modal);
                background: var(--lx-scrim);
                -webkit-backdrop-filter: blur(var(--lx-blur-xs));
                backdrop-filter: blur(var(--lx-blur-xs));
                display: flex;
                align-items: center;
                justify-content: center;
            }}
            .lx-modal {{
                background: var(--lx-surface-glass);
                -webkit-backdrop-filter: blur(var(--lx-blur-md)) saturate(var(--lx-glass-saturate));
                backdrop-filter: blur(var(--lx-blur-md)) saturate(var(--lx-glass-saturate));
                border: 1px solid var(--lx-border-soft);
                border-radius: var(--lx-radius-lg);
                box-shadow: var(--lx-shadow-lg);
            }}

            .lx-status-rail {{
                width: var(--lx-rail-width);
                align-self: stretch;
                flex-shrink: 0;
                border-radius: var(--lx-radius-full);
                background: var(--lx-state-unknown);
            }}
            .lx-status-rail--up {{ background: var(--lx-state-up); }}
            .lx-status-rail--down {{ background: var(--lx-state-down); }}
            .lx-status-rail--paused {{ background: var(--lx-state-paused); }}
            .lx-status-rail--success {{ background: var(--lx-state-success); }}

            .lx-row-hover {{ transition: background var(--lx-transition-fast) var(--lx-ease); }}
            .lx-row-hover:hover {{ background: color-mix(in srgb, var(--lx-elevated) 55%, transparent); }}

            .lx-stepper {{ display: flex; align-items: center; gap: calc(var(--lx-space-unit) * 2); }}
            .lx-stepper__step {{
                display: inline-flex; align-items: center; justify-content: center;
                width: var(--lx-icon-lg); height: var(--lx-icon-lg);
                border-radius: var(--lx-radius-full);
                font-size: var(--lx-text-2xs); font-weight: 600;
                color: var(--lx-text-muted);
                background: var(--lx-elevated);
                border: 1px solid var(--lx-border-soft);
            }}
            .lx-stepper__step--active {{
                color: var(--lx-on-accent-text); background: var(--lx-accent); border-color: var(--lx-accent);
            }}
            .lx-stepper__step--done {{
                color: var(--lx-on-accent-text); background: var(--lx-state-success); border-color: var(--lx-state-success);
            }}
            .lx-stepper__bar {{ flex: 1; height: var(--lx-border-width); background: var(--lx-border-soft); }}

            .lx-entity-card {{
                background: var(--lx-surface-glass);
                border: 1px solid var(--lx-border-soft);
                border-radius: var(--lx-radius-lg);
                padding: calc(var(--lx-space-unit) * 4);
                transition: border-color var(--lx-transition-fast) var(--lx-ease);
            }}
            .lx-entity-card:hover {{ border-color: var(--lx-border); }}
            .lx-iframe-frame {{
                border: var(--lx-border-width) solid var(--lx-border-soft);
                border-radius: var(--lx-radius-md);
                background: var(--lx-surface);
                overflow: hidden;
            }}

            /* Badge family — mirrors lyndrix-ui/src/index.css exactly */
            .lx-badge {{
                display: inline-flex; align-items: center;
                gap: calc(var(--lx-space-unit) * 1.25);
                font-size: 0.6875rem; font-weight: 600; line-height: 1.2;
                padding: calc(var(--lx-space-unit) * 0.75) calc(var(--lx-space-unit) * 2.25);
                border-radius: var(--lx-radius-full);
                border: 1px solid transparent;
                white-space: nowrap;
            }}
            .lx-badge .lx-dot {{ width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; background: currentColor; }}
            .lx-badge--up {{ color: var(--lx-state-up); background: color-mix(in srgb, var(--lx-state-up) 13%, transparent); border-color: color-mix(in srgb, var(--lx-state-up) 28%, transparent); }}
            .lx-badge--down {{ color: var(--lx-state-down); background: color-mix(in srgb, var(--lx-state-down) 13%, transparent); border-color: color-mix(in srgb, var(--lx-state-down) 28%, transparent); }}
            .lx-badge--paused {{ color: var(--lx-state-paused); background: color-mix(in srgb, var(--lx-state-paused) 13%, transparent); border-color: color-mix(in srgb, var(--lx-state-paused) 28%, transparent); }}
            .lx-badge--unknown {{ color: var(--lx-state-unknown); background: color-mix(in srgb, var(--lx-state-unknown) calc(var(--lx-badge-bg-opacity) * 100%), transparent); border-color: color-mix(in srgb, var(--lx-state-unknown) calc(var(--lx-badge-border-opacity) * 100%), transparent); }}
            .lx-badge--success {{ color: var(--lx-state-success); background: color-mix(in srgb, var(--lx-state-success) calc(var(--lx-badge-bg-opacity) * 100%), transparent); border-color: color-mix(in srgb, var(--lx-state-success) calc(var(--lx-badge-border-opacity) * 100%), transparent); }}
            .lx-badge--accent {{ color: var(--lx-accent); background: color-mix(in srgb, var(--lx-accent) 13%, transparent); border-color: color-mix(in srgb, var(--lx-accent) 28%, transparent); }}
            .lx-badge--muted {{ color: var(--lx-text-muted); background: var(--lx-elevated); border-color: var(--lx-border-soft); }}

            ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
            ::-webkit-scrollbar-track {{ background: transparent; }}
            body.body--dark ::-webkit-scrollbar-thumb, html.dark ::-webkit-scrollbar-thumb {{
                background: color-mix(in srgb, var(--lx-accent) 25%, transparent);
                border-radius: var(--lx-radius);
            }}
            body.body--light ::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: var(--lx-radius); }}
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