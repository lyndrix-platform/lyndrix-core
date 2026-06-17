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
