from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from config import settings
from core.theming.loader import load_theme_pack
from core.theming.models import ThemePack


THEMES_BASE_DIR = Path(__file__).resolve().parents[2] / "assets" / "themes"


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
        clean semantic source (alpha borders, glass, glow, radii, log palette)
        use canonical literals. Any ``tokens.json`` ``css_variables[mode]``
        overrides are applied last. Pure — no side effects.
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
                "--lx-glow": "0 0 30px rgba(0, 212, 255, 0.3)",
                "--lx-radius-sm": "6px",
                "--lx-radius-md": "12px",
                "--lx-radius-lg": "20px",
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
                "--lx-glow": "0 0 20px rgba(8, 145, 178, 0.12)",
                "--lx-radius-sm": "6px",
                "--lx-radius-md": "12px",
                "--lx-radius-lg": "20px",
            }

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
