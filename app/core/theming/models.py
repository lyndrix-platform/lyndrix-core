from __future__ import annotations

from pydantic import BaseModel, Field


class ThemeColorModes(BaseModel):
    light: str
    dark: str


class ThemeCssVariables(BaseModel):
    """Optional per-theme ``--lx-*`` overrides, keyed by colour mode.

    Themes may pin specific CSS custom properties precisely instead of relying
    on the deterministic derivation in ``ThemeEngine.resolve_css_variables``.
    Both maps are optional and default empty, so partial themes stay valid.
    """

    light: dict[str, str] = Field(default_factory=dict)
    dark: dict[str, str] = Field(default_factory=dict)


class ThemeBackgroundImages(BaseModel):
    """Optional per-theme background image filenames, keyed by colour mode.

    Values are filenames relative to the theme's ``assets/`` directory
    (e.g. ``"background-dark.webp"``); both are optional so a theme may ship
    only one mode or none at all.
    """

    light: str | None = None
    dark: str | None = None


class ThemeTokens(BaseModel):
    colors: dict[str, ThemeColorModes] = Field(default_factory=dict)
    css_variables: ThemeCssVariables | None = None
    background_images: ThemeBackgroundImages | None = None


class ThemeComponents(BaseModel):
    styles: dict[str, str] = Field(default_factory=dict)


class ThemePack(BaseModel):
    theme_id: str
    tokens: ThemeTokens
    components: ThemeComponents
