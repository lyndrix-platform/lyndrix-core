from __future__ import annotations

from pydantic import BaseModel, Field


class ThemeColorModes(BaseModel):
    light: str
    dark: str


class ThemeTokens(BaseModel):
    colors: dict[str, ThemeColorModes] = Field(default_factory=dict)


class ThemeComponents(BaseModel):
    styles: dict[str, str] = Field(default_factory=dict)


class ThemePack(BaseModel):
    theme_id: str
    tokens: ThemeTokens
    components: ThemeComponents
