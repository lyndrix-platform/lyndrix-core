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
    "--lx-elevated-glass", "--lx-glass-border",
    "--lx-accent", "--lx-accent-2", "--lx-accent-3",
    "--lx-text", "--lx-text-muted",
    "--lx-border", "--lx-border-soft", "--lx-glow",
    "--lx-radius-sm", "--lx-radius", "--lx-radius-md", "--lx-radius-lg",
    "--lx-radius-xl", "--lx-radius-2xl", "--lx-radius-3xl", "--lx-radius-full",
    "--lx-space-unit",
    "--lx-font-sans", "--lx-font-mono", "--lx-text-scale",
    "--lx-shadow-sm", "--lx-shadow-md", "--lx-shadow-lg", "--lx-shadow-glow",
    "--lx-blur-sm", "--lx-blur-md", "--lx-blur-lg",
    "--lx-blur-xs",
    "--lx-transition-fast", "--lx-transition-base", "--lx-transition-slow", "--lx-ease",
    "--lx-gradient-accent",
    "--lx-gradient-success", "--lx-gradient-warning", "--lx-gradient-info", "--lx-gradient-danger",
    "--lx-border-width", "--lx-rail-width",
    "--lx-state-up", "--lx-state-down", "--lx-state-paused", "--lx-state-unknown",
    "--lx-state-success", "--lx-state-marked", "--lx-state-maintenance",
    "--lx-warning", "--lx-log-bg", "--lx-log-fg", "--lx-log-accent",
    "--lx-terminal-bg", "--lx-terminal-fg", "--lx-terminal-accent",
    "--lx-chart-1", "--lx-chart-2", "--lx-chart-3", "--lx-chart-4",
    "--lx-chart-5", "--lx-chart-6", "--lx-chart-7", "--lx-chart-8",
    "--lx-icon-xs", "--lx-icon-sm", "--lx-icon-md", "--lx-icon-lg",
    "--lx-z-dropdown", "--lx-z-modal", "--lx-z-toast", "--lx-scrim",
    "--lx-text-2xs", "--lx-text-3xs",
    "--lx-glass-saturate", "--lx-on-accent-text", "--lx-on-accent-text-light",
    "--lx-badge-bg-opacity", "--lx-badge-border-opacity",
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
# Characters / constructs that could break out of the ``<style>`` block or the
# declaration these values are interpolated into, or make the browser fetch an
# attacker-chosen URL: markup and block delimiters, the declaration terminator,
# escapes (which can spell any of the others), comments and resource functions.
_UNSAFE_CSS_CHARS_RE = re.compile(r"[<>{};\\\x00-\x1f\x7f]")
_UNSAFE_CSS_CONSTRUCTS = ("url(", "expression(", "@import", "/*", "*/", "javascript:")
_MAX_CSS_VALUE_LENGTH = 1000

# Strict colour grammar (SC-02): colours are also written into an HTML attribute
# (``<meta name="theme-color">``), so only literal colour notations are accepted.
_CSS_COLOR_RE = re.compile(
    r"^(?:"
    r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
    r"|(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\([0-9a-zA-Z.,%/ +-]{1,80}\)"
    r"|[a-zA-Z]{3,24}"  # named colours, ``transparent``, ``currentColor``
    r")$"
)
_CSS_VAR_NAME_RE = re.compile(r"^--[A-Za-z0-9_-]{1,64}$")
_ASSET_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
THEME_IMAGE_SUFFIXES: frozenset[str] = frozenset({".webp", ".png", ".jpg", ".jpeg"})


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
    block, so reject everything that could end the declaration, leave the
    block, or load a remote resource. Quotes are fine (font names) as long as
    they are balanced.
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or len(v) > _MAX_CSS_VALUE_LENGTH:
        return False
    if _UNSAFE_CSS_CHARS_RE.search(v):
        return False
    lowered = v.lower().replace(" ", "")
    if any(marker in lowered for marker in _UNSAFE_CSS_CONSTRUCTS):
        return False
    return v.count('"') % 2 == 0 and v.count("'") % 2 == 0


def is_valid_css_color(value: str) -> bool:
    """True for a literal colour: hex, ``rgb()/hsl()/oklch()``-style or a name."""
    return isinstance(value, str) and bool(_CSS_COLOR_RE.match(value.strip()))


def is_valid_css_var_name(name: str) -> bool:
    """True for a custom-property name such as ``--lx-accent``."""
    return isinstance(name, str) and bool(_CSS_VAR_NAME_RE.fullmatch(name))


def is_safe_asset_filename(name: str) -> bool:
    """True for a plain image filename inside a theme's ``assets/`` directory."""
    if not isinstance(name, str) or not _ASSET_FILENAME_RE.fullmatch(name) or ".." in name:
        return False
    return any(name.lower().endswith(suffix) for suffix in THEME_IMAGE_SUFFIXES)


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
        # calc(...) micro-type values — shape too varied to length-check.
        "text_2xs": is_safe_css_value,
        "text_3xs": is_safe_css_value,
    },
    "shadow": {key: is_safe_css_value for key in ("sm", "md", "lg", "glow")},
    "blur": {key: is_valid_css_length for key in ("xs", "sm", "md", "lg")},
    "transition": {
        "fast": is_valid_css_duration,
        "base": is_valid_css_duration,
        "slow": is_valid_css_duration,
        "ease": is_safe_css_value,
    },
    "gradient": {
        key: is_safe_css_value
        for key in ("accent", "success", "warning", "info", "danger")
    },
    "border": {"width": is_valid_css_length, "rail": is_valid_css_length},
    "icon": {key: is_valid_css_length for key in ("xs", "sm", "md", "lg")},
    # z-index values are bare integers.
    "zindex": {key: is_valid_css_number for key in ("dropdown", "modal", "toast")},
    "effect": {
        # glass_saturate is a percentage (is_valid_css_length accepts `%`);
        # the badge opacities are unitless 0..1 numbers.
        "glass_saturate": is_valid_css_length,
        "badge_bg_opacity": is_valid_css_number,
        "badge_border_opacity": is_valid_css_number,
    },
}


def _required_component_keys() -> frozenset[str]:
    from ui.theme import UIStyles
    return frozenset(
        k for k in vars(UIStyles)
        if k.isupper() and not k.startswith("_")
    )


def theme_pack_errors(pack: "ThemePack") -> list[str]:
    """Values that must never reach a page: the hard-error half of validation.

    A theme is data, but its colours, ``css_variables`` and background image
    names are written into ``<style>`` / HTML attributes on every page (SC-02).
    The upload API rejects a pack with any of these (422); for a pack already
    on disk the engine ignores the offending value and uses the default.
    """
    errors: list[str] = []
    prefix = f"Theme '{pack.theme_id}'"

    for key, modes in pack.tokens.colors.items():
        for mode in ("light", "dark"):
            value = getattr(modes, mode)
            if not is_valid_css_color(value):
                errors.append(
                    f"{prefix}: colors.{key}.{mode} is not a valid colour "
                    f"(use #hex, rgb()/hsl()/oklch() or a colour name)"
                )

    css_vars = pack.tokens.css_variables
    if css_vars is not None:
        for mode in ("light", "dark"):
            for key, value in (getattr(css_vars, mode) or {}).items():
                if not is_valid_css_var_name(key):
                    # The key itself is not echoed: it is attacker-controlled
                    # text that would land in logs and API responses.
                    errors.append(
                        f"{prefix}: css_variables.{mode} key is not a custom "
                        f"property name (expected --name)"
                    )
                elif not is_safe_css_value(value):
                    errors.append(
                        f"{prefix}: css_variables.{mode}.{key} has an unsafe value "
                        f"(no ; {{ }} < > \\ url() @import or comments)"
                    )

    bg_images = pack.tokens.background_images
    if bg_images is not None:
        for mode in ("light", "dark"):
            filename = getattr(bg_images, mode)
            if filename and not is_safe_asset_filename(filename):
                errors.append(
                    f"{prefix}: background_images.{mode} must be a plain image file "
                    f"name from assets/ (.webp, .png, .jpg, .jpeg)"
                )

    return errors


def validate_theme_pack(pack: "ThemePack") -> list[str]:
    """Validate *pack* and backfill missing keys with UIStyles defaults.

    Returns a (possibly empty) list of warning strings — one per missing key.
    The pack is mutated in place so that downstream code always has a full set
    of keys regardless of what the JSON file contained.
    """
    from ui.theme import UIStyles

    # Unsafe values are reported first; the engine drops them at resolve time.
    warnings: list[str] = [f"{error} — ignored" for error in theme_pack_errors(pack)]

    # --- token colours ---
    for key in REQUIRED_TOKEN_KEYS:
        if key not in pack.tokens.colors:
            warnings.append(f"Theme '{pack.theme_id}': missing token color '{key}'")

    # --- optional css_variables overrides (best-effort typo guard) ---
    css_vars = getattr(pack.tokens, "css_variables", None)
    if css_vars is not None:
        for mode in ("light", "dark"):
            for key in getattr(css_vars, mode, {}) or {}:
                if key not in KNOWN_CSS_VAR_KEYS and is_valid_css_var_name(key):
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
