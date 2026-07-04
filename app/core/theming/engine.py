from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import settings
from core.theming.loader import load_theme_pack
from core.theming.models import ThemePack
from core.theming.schema import TOKEN_VALUE_VALIDATORS

log = logging.getLogger("Core:ThemeEngine")

THEMES_BASE_DIR = Path(__file__).resolve().parents[2] / "assets" / "themes"


# --- Theming v2 Phase-1 token categories -----------------------------------
# Exact defaults from the token contract — chosen to reproduce today's
# hardcoded look, so a pack that omits a category (or a key within one)
# falls back to these values with zero visual change. Each category is a
# flat ``key -> CSS value`` map (no light/dark split; the mode split lives
# only in ``colors``). ``(defaults, var_names)`` pairs share keys so the
# resolver below can zip them generically.

_RADIUS_DEFAULTS: dict[str, str] = {
    "sm": "0.125rem",
    "DEFAULT": "0.25rem",
    "md": "0.375rem",
    "lg": "0.5rem",
    "xl": "0.75rem",
    "2xl": "1rem",
    "3xl": "1.5rem",
    "full": "9999px",
}
_RADIUS_VARS: dict[str, str] = {
    "sm": "--lx-radius-sm",
    "DEFAULT": "--lx-radius",
    "md": "--lx-radius-md",
    "lg": "--lx-radius-lg",
    "xl": "--lx-radius-xl",
    "2xl": "--lx-radius-2xl",
    "3xl": "--lx-radius-3xl",
    "full": "--lx-radius-full",
}

_SPACING_DEFAULTS: dict[str, str] = {"unit": "0.25rem"}
_SPACING_VARS: dict[str, str] = {"unit": "--lx-space-unit"}

_TYPOGRAPHY_DEFAULTS: dict[str, str] = {
    "font_sans": '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    "font_mono": '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, monospace',
    "text_scale": "1",
}
_TYPOGRAPHY_VARS: dict[str, str] = {
    "font_sans": "--lx-font-sans",
    "font_mono": "--lx-font-mono",
    "text_scale": "--lx-text-scale",
}

_SHADOW_DEFAULTS: dict[str, str] = {
    "sm": "0 1px 2px 0 rgb(0 0 0 / 0.25)",
    "md": "0 8px 24px -12px rgb(0 0 0 / 0.45)",
    "lg": "0 30px 80px -20px rgb(0 0 0 / 0.6)",
    "glow": "0 0 30px rgb(0 212 255 / 0.3)",
}
_SHADOW_VARS: dict[str, str] = {
    "sm": "--lx-shadow-sm",
    "md": "--lx-shadow-md",
    "lg": "--lx-shadow-lg",
    "glow": "--lx-shadow-glow",
}

_BLUR_DEFAULTS: dict[str, str] = {"sm": "8px", "md": "16px", "lg": "24px"}
_BLUR_VARS: dict[str, str] = {
    "sm": "--lx-blur-sm", "md": "--lx-blur-md", "lg": "--lx-blur-lg",
}

_TRANSITION_DEFAULTS: dict[str, str] = {
    "fast": "150ms",
    "base": "250ms",
    "slow": "400ms",
    "ease": "cubic-bezier(0.4, 0, 0.2, 1)",
}
_TRANSITION_VARS: dict[str, str] = {
    "fast": "--lx-transition-fast",
    "base": "--lx-transition-base",
    "slow": "--lx-transition-slow",
    "ease": "--lx-ease",
}

_GRADIENT_DEFAULTS: dict[str, str] = {
    "accent": "linear-gradient(135deg, var(--lx-accent), var(--lx-accent-2), var(--lx-accent-3))",
}
_GRADIENT_VARS: dict[str, str] = {"accent": "--lx-gradient-accent"}

_BORDER_DEFAULTS: dict[str, str] = {"width": "1px"}
_BORDER_VARS: dict[str, str] = {"width": "--lx-border-width"}

# (category name, defaults, var names) — iterated by _resolve_token_category.
_TOKEN_CATEGORIES: tuple[tuple[str, dict[str, str], dict[str, str]], ...] = (
    ("radius", _RADIUS_DEFAULTS, _RADIUS_VARS),
    ("spacing", _SPACING_DEFAULTS, _SPACING_VARS),
    ("typography", _TYPOGRAPHY_DEFAULTS, _TYPOGRAPHY_VARS),
    ("shadow", _SHADOW_DEFAULTS, _SHADOW_VARS),
    ("blur", _BLUR_DEFAULTS, _BLUR_VARS),
    ("transition", _TRANSITION_DEFAULTS, _TRANSITION_VARS),
    ("gradient", _GRADIENT_DEFAULTS, _GRADIENT_VARS),
    ("border", _BORDER_DEFAULTS, _BORDER_VARS),
)


class ThemeEngine:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or THEMES_BASE_DIR
        # plugin_id → {STYLE_KEY: "tailwind classes", ...}
        self._plugin_overrides: dict[str, dict[str, str]] = {}

    def resolve_theme(self, theme_id: str | None = None) -> ThemePack:
        active_id = theme_id or settings.DEFAULT_THEME_ID
        try:
            return load_theme_pack(self.base_dir, active_id)
        except Exception:
            return load_theme_pack(self.base_dir, settings.DEFAULT_THEME_ID)

    def resolve_color(self, key: str, dark: bool, theme_id: str | None = None) -> str | None:
        theme = self.resolve_theme(theme_id)
        item = theme.tokens.colors.get(key)
        if not item:
            return None
        return item.dark if dark else item.light

    def resolve_component_styles(
        self,
        theme_id: str | None = None,
        plugin_id: str | None = None,
    ) -> dict[str, str]:
        """Return merged styles: base theme + optional plugin-level overrides."""
        base = dict(self.resolve_theme(theme_id).components.styles)
        if plugin_id and plugin_id in self._plugin_overrides:
            base.update(self._plugin_overrides[plugin_id])
        return base

    def resolve_runtime_palette(self, dark: bool, theme_id: str | None = None) -> dict[str, Any]:
        theme = self.resolve_theme(theme_id)
        colors = theme.tokens.colors

        def color(name: str, fallback: str) -> str:
            c = colors.get(name)
            if not c:
                return fallback
            return c.dark if dark else c.light

        return {
            "primary": color("primary", "#6366f1"),
            "secondary": color("secondary", "#0ea5e9"),
            "accent": color("accent", "#8b5cf6"),
            "positive": color("positive", "#22c55e"),
            "negative": color("negative", "#ef4444"),
            "info": color("info", "#3b82f6"),
            "warning": color("warning", "#f59e0b"),
        }

    def resolve_css_variables(self, dark: bool, theme_id: str | None = None) -> dict[str, str]:
        """Resolve the full ``--lx-*`` CSS custom-property map for one colour mode.

        This is the single source of truth for the Lyndrix design tokens that
        both the React UI (over HTTP) and NiceGUI consume. Values are derived
        from the theme's semantic ``colors`` where a clean mapping exists; each
        with a hard fallback equal to the canonical Lyndrix UI palette so the
        ``default`` theme reproduces today's look exactly. Variables without a
        clean semantic source (alpha borders, glass, log palette) use
        canonical literals. The Theming v2 Phase-1 categories — radius,
        spacing, typography, shadow, blur, transition, gradient, border — are
        resolved separately via ``_resolve_token_category`` with contract
        defaults as backfill (same value in both colour modes). Any
        ``tokens.json`` ``css_variables[mode]`` overrides are applied last.
        Pure — no side effects.
        """
        theme = self.resolve_theme(theme_id)
        colors = theme.tokens.colors

        def c(name: str, fallback: str) -> str:
            item = colors.get(name)
            if not item:
                return fallback
            return item.dark if dark else item.light

        if dark:
            css: dict[str, str] = {
                # --- derived from semantic colour tokens ---
                "--lx-bg": c("bg_body", "#0a0e1a"),
                "--lx-surface": c("bg_surface", "#0f1629"),
                "--lx-elevated": c("bg_elevated", "#162040"),
                "--lx-accent": c("primary", "#00d4ff"),
                "--lx-accent-2": c("secondary", "#0ea5e9"),
                "--lx-accent-3": c("accent", "#8b5cf6"),
                "--lx-text": c("text_body", "#f0f6ff"),
                "--lx-text-muted": c("text_muted", "#94a3b8"),
                "--lx-state-up": c("positive", "#22d3ee"),
                "--lx-state-down": c("negative", "#f87171"),
                "--lx-state-paused": c("warning", "#fbbf24"),
                "--lx-warning": c("warning", "#fbbf24"),
                "--lx-state-unknown": c("text_subtle", "#94a3b8"),
                # --- canonical literals (no clean semantic source) ---
                "--lx-surface-glass": "rgba(15, 22, 41, 0.58)",
                "--lx-border": "rgba(0, 212, 255, 0.2)",
                "--lx-border-soft": "rgba(255, 255, 255, 0.08)",
            }
        else:
            css = {
                "--lx-bg": c("bg_body", "#f1f5f9"),
                "--lx-surface": c("bg_surface", "#ffffff"),
                "--lx-elevated": c("bg_elevated", "#e2e8f0"),
                "--lx-accent": c("primary", "#0891b2"),
                "--lx-accent-2": c("secondary", "#0284c7"),
                "--lx-accent-3": c("accent", "#7c3aed"),
                "--lx-text": c("text_body", "#0f172a"),
                "--lx-text-muted": c("text_muted", "#64748b"),
                "--lx-state-up": c("positive", "#0891b2"),
                "--lx-state-down": c("negative", "#ef4444"),
                "--lx-state-paused": c("warning", "#f59e0b"),
                "--lx-warning": c("warning", "#f59e0b"),
                "--lx-state-unknown": c("text_subtle", "#94a3b8"),
                "--lx-surface-glass": "rgba(255, 255, 255, 0.72)",
                "--lx-border": "rgba(8, 145, 178, 0.3)",
                "--lx-border-soft": "rgba(0, 0, 0, 0.09)",
            }

        # Theming v2 Phase-1 categories: same value in both colour modes,
        # each key individually backfilled from the contract default if the
        # pack's tokens.json omits the category/key or the value fails cheap
        # validation. This replaces the old dead --lx-radius-sm/md/lg
        # literals above (6/12/20px, nothing consumed them) with the real,
        # Tailwind-aligned scale.
        for category, defaults, var_names in _TOKEN_CATEGORIES:
            css.update(self._resolve_token_category(theme, category, defaults, var_names))

        # --lx-glow predates the shadow category and stays as an alias of
        # --lx-shadow-glow so existing consumers keep working unchanged.
        css["--lx-glow"] = css["--lx-shadow-glow"]

        # Log palette mirrors the page surface so terminals match the theme.
        css["--lx-log-bg"] = css["--lx-bg"]
        css["--lx-log-fg"] = css["--lx-text"]
        css["--lx-log-accent"] = css["--lx-state-up"]

        # Optional themed background image (mode-resolved). Served by the asset
        # route at /api/themes/{id}/assets/{file}. Emitted before css_variables
        # so an explicit per-theme override still wins. Default is "none".
        bg_images = getattr(theme.tokens, "background_images", None)
        bg_file = None
        if bg_images is not None:
            bg_file = bg_images.dark if dark else bg_images.light
        if bg_file:
            active_id = theme_id or settings.DEFAULT_THEME_ID
            url = f"/api/themes/{active_id}/assets/{bg_file}"
            # The asset is served `Cache-Control: immutable` (1y), so swapping the
            # image in place — same filename — would otherwise never reach clients
            # that already cached it. Fingerprint the URL with the file's
            # mtime+size (mirrors the per-user background `?v=` cache-buster) so a
            # content change yields a new URL and busts the immutable cache.
            try:
                st = (self.base_dir / active_id / "assets" / bg_file).stat()
                url += f"?v={int(st.st_mtime)}-{st.st_size}"
            except OSError:
                pass
            css["--lx-bg-image"] = f'url("{url}")'
        else:
            css["--lx-bg-image"] = "none"
        css["--lx-bg-image-size"] = "cover"
        css["--lx-bg-image-position"] = "center"

        # Optional per-theme overrides from tokens.json css_variables[mode].
        overrides = getattr(theme.tokens, "css_variables", None)
        if overrides is not None:
            mode_overrides = overrides.dark if dark else overrides.light
            for key, value in (mode_overrides or {}).items():
                css[str(key)] = str(value)

        return css

    @staticmethod
    def _resolve_token_category(
        theme: ThemePack,
        category: str,
        defaults: dict[str, str],
        var_names: dict[str, str],
    ) -> dict[str, str]:
        """Backfill one Phase-1 token category into ``--lx-*`` CSS vars.

        For each key in *defaults*: use the pack's ``tokens.json`` value if
        present and it passes the category's cheap value validator, else fall
        back to the contract default. Never raises — a missing category, a
        missing key, or an invalid value all resolve to the default, so a v1
        colour-only pack (or one with a typo) still renders correctly.
        """
        values = getattr(theme.tokens, category, None) or {}
        validators = TOKEN_VALUE_VALIDATORS.get(category, {})
        resolved: dict[str, str] = {}
        for key, default in defaults.items():
            var_name = var_names[key]
            raw = values.get(key)
            if raw is None:
                resolved[var_name] = default
                continue
            raw_str = str(raw)
            validator = validators.get(key)
            if validator is not None and not validator(raw_str):
                log.warning(
                    "Theme '%s': invalid value for %s.%s (%r) — using default %r",
                    theme.theme_id, category, key, raw_str, default,
                )
                resolved[var_name] = default
            else:
                resolved[var_name] = raw_str
        return resolved

    def register_plugin_overrides(self, plugin_id: str, overrides: dict[str, str]) -> None:
        """Register partial UIStyles overrides for a specific plugin.

        Call via ctx.register_theme_overrides() in the plugin's setup().
        Overrides apply only when resolve_component_styles() is called with
        the matching plugin_id — they do not affect the global UIStyles class.
        """
        self._plugin_overrides[plugin_id] = dict(overrides)

    def list_available_themes(self) -> list[str]:
        """Return all theme IDs available on disk."""
        if not self.base_dir.exists():
            return ["default"]
        return sorted(
            d.name
            for d in self.base_dir.iterdir()
            if d.is_dir() and (d / "tokens.json").exists()
        )


@lru_cache(maxsize=1)
def get_theme_engine() -> ThemeEngine:
    return ThemeEngine()
