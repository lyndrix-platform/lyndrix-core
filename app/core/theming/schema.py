"""Theme validation helpers.

``validate_theme_pack`` checks that a loaded ThemePack contains all keys
required by the current UIStyles class and token colour palette.  Missing keys
are filled with the class-level defaults so loading always succeeds — the
caller receives a list of warning strings to log or surface in the UI.
"""
from __future__ import annotations

import re
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from core.theming.models import ThemePack

REQUIRED_TOKEN_KEYS: frozenset[str] = frozenset({
    "primary", "secondary", "accent",
    "positive", "negative", "info", "warning",
    "bg_body", "bg_surface", "bg_elevated",
    "text_body", "text_muted", "text_subtle",
})

# Recognised ``--lx-*`` custom properties an optional css_variables override may
# pin. Not required (themes need none); used only to warn on likely typos.
KNOWN_CSS_VAR_KEYS: frozenset[str] = frozenset({
    "--lx-bg", "--lx-surface", "--lx-surface-glass", "--lx-elevated",
    "--lx-accent", "--lx-accent-2", "--lx-accent-3",
    "--lx-text", "--lx-text-muted",
    "--lx-border", "--lx-border-soft", "--lx-glow",
    "--lx-radius-sm", "--lx-radius", "--lx-radius-md", "--lx-radius-lg",
    "--lx-radius-xl", "--lx-radius-2xl", "--lx-radius-3xl", "--lx-radius-full",
    "--lx-space-unit",
    "--lx-font-sans", "--lx-font-mono", "--lx-text-scale",
    "--lx-shadow-sm", "--lx-shadow-md", "--lx-shadow-lg", "--lx-shadow-glow",
    "--lx-blur-sm", "--lx-blur-md", "--lx-blur-lg",
    "--lx-transition-fast", "--lx-transition-base", "--lx-transition-slow", "--lx-ease",
    "--lx-gradient-accent",
    "--lx-border-width",
    "--lx-state-up", "--lx-state-down", "--lx-state-paused", "--lx-state-unknown",
    "--lx-warning", "--lx-log-bg", "--lx-log-fg", "--lx-log-accent",
})


# --- Cheap, non-fatal CSS value-level validation (Theming v2 Phase 1) -----
# These check *shape*, not full CSS grammar — good enough to catch obvious
# typos/garbage in an uploaded theme pack without becoming a CSS parser.
# Callers treat a failed check as "use the built-in default", never as a
# hard error, so a pack with one bad value still loads.

_CSS_LENGTH_RE = re.compile(
    r"^-?\d+(\.\d+)?(px|rem|em|%|vh|vw|vmin|vmax|ch|ex|pt|pc|in|cm|mm)$"
)
_CSS_DURATION_RE = re.compile(r"^-?\d+(\.\d+)?(ms|s)$")
_CSS_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_UNSAFE_CSS_CHARS_RE = re.compile(r"[<>{}]")


def is_valid_css_length(value: str) -> bool:
    """True for a plain CSS length like ``0.25rem``, ``9999px`` or ``0``."""
    v = value.strip()
    if v in ("0", "0px"):
        return True
    return bool(_CSS_LENGTH_RE.match(v))


def is_valid_css_duration(value: str) -> bool:
    """True for a plain CSS time value like ``150ms`` or ``0.4s``."""
    return bool(_CSS_DURATION_RE.match(value.strip()))


def is_valid_css_number(value: str) -> bool:
    """True for a bare number (e.g. a unitless scale factor like ``1``)."""
    return bool(_CSS_NUMBER_RE.match(value.strip()))


def is_safe_css_value(value: str) -> bool:
    """True for a non-empty free-form CSS value with no injection markers.

    Used for values too varied to shape-check precisely (font stacks,
    box-shadow lists, gradients, easing functions) — themes may be uploaded
    as a ZIP, and these strings are interpolated directly into a ``<style>``
    block, so reject characters that could break out of it.
    """
    v = value.strip()
    if not v:
        return False
    return not _UNSAFE_CSS_CHARS_RE.search(v)


# Per-category, per-key validator for the Phase-1 token categories. Shared by
# ThemeEngine (backfill decisions) and validate_theme_pack (load-time
# warnings) so both agree on what "valid" means.
TOKEN_VALUE_VALIDATORS: dict[str, dict[str, Callable[[str], bool]]] = {
    "radius": {
        key: is_valid_css_length
        for key in ("sm", "DEFAULT", "md", "lg", "xl", "2xl", "3xl", "full")
    },
    "spacing": {"unit": is_valid_css_length},
    "typography": {
        "font_sans": is_safe_css_value,
        "font_mono": is_safe_css_value,
        "text_scale": is_valid_css_number,
    },
    "shadow": {key: is_safe_css_value for key in ("sm", "md", "lg", "glow")},
    "blur": {key: is_valid_css_length for key in ("sm", "md", "lg")},
    "transition": {
        "fast": is_valid_css_duration,
        "base": is_valid_css_duration,
        "slow": is_valid_css_duration,
        "ease": is_safe_css_value,
    },
    "gradient": {"accent": is_safe_css_value},
    "border": {"width": is_valid_css_length},
}


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

    # --- optional css_variables overrides (best-effort typo guard) ---
    css_vars = getattr(pack.tokens, "css_variables", None)
    if css_vars is not None:
        for mode in ("light", "dark"):
            for key in getattr(css_vars, mode, {}) or {}:
                if key not in KNOWN_CSS_VAR_KEYS:
                    warnings.append(
                        f"Theme '{pack.theme_id}': unknown css_variables.{mode} "
                        f"key '{key}' (not a recognised --lx-* property)"
                    )

    # --- Phase-1 token categories (radius/spacing/typography/shadow/blur/
    # transition/gradient/border) — optional, best-effort typo + value guard.
    # Never mutated here: ThemeEngine independently backfills any missing or
    # invalid key with the contract default at resolve time, so these are
    # warnings only, not corrections.
    for category, validators in TOKEN_VALUE_VALIDATORS.items():
        values = getattr(pack.tokens, category, None) or {}
        for key, value in values.items():
            validator = validators.get(key)
            if validator is None:
                warnings.append(
                    f"Theme '{pack.theme_id}': unknown {category}.{key} key "
                    f"(not a recognised token)"
                )
            elif not validator(str(value)):
                warnings.append(
                    f"Theme '{pack.theme_id}': invalid value for {category}.{key} "
                    f"({value!r}) — falling back to the built-in default"
                )

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
