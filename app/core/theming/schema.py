"""Theme validation helpers.

``validate_theme_pack`` checks that a loaded ThemePack contains all keys
required by the current UIStyles class and token colour palette.  Missing keys
are filled with the class-level defaults so loading always succeeds — the
caller receives a list of warning strings to log or surface in the UI.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.theming.models import ThemePack

REQUIRED_TOKEN_KEYS: frozenset[str] = frozenset({
    "primary", "secondary", "accent",
    "positive", "negative", "info", "warning",
    "bg_body", "bg_surface", "bg_elevated",
    "text_body", "text_muted", "text_subtle",
})


def _required_component_keys() -> frozenset[str]:
    from ui.theme import UIStyles
    return frozenset(
        k for k in vars(UIStyles)
        if k.isupper() and not k.startswith("_")
    )


def validate_theme_pack(pack: "ThemePack") -> list[str]:
    """Validate *pack* and backfill missing keys with UIStyles defaults.

    Returns a (possibly empty) list of warning strings — one per missing key.
    The pack is mutated in place so that downstream code always has a full set
    of keys regardless of what the JSON file contained.
    """
    from ui.theme import UIStyles

    warnings: list[str] = []

    # --- token colours ---
    for key in REQUIRED_TOKEN_KEYS:
        if key not in pack.tokens.colors:
            warnings.append(f"Theme '{pack.theme_id}': missing token color '{key}'")

    # --- component styles ---
    required = _required_component_keys()
    for key in required:
        if key not in pack.components.styles:
            fallback = getattr(UIStyles, key, "")
            pack.components.styles[key] = fallback
            warnings.append(
                f"Theme '{pack.theme_id}': missing component style '{key}', "
                f"using UIStyles default"
            )

    return warnings
