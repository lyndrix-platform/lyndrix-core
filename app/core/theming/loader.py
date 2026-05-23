from __future__ import annotations

import json
from pathlib import Path

from core.theming.models import ThemeComponents, ThemePack, ThemeTokens


def load_theme_pack(base_dir: Path, theme_id: str) -> ThemePack:
    theme_dir = base_dir / theme_id
    tokens_path = theme_dir / "tokens.json"
    components_path = theme_dir / "components.json"

    with tokens_path.open("r", encoding="utf-8") as f:
        tokens_data = json.load(f)

    with components_path.open("r", encoding="utf-8") as f:
        components_data = json.load(f)

    return ThemePack(
        theme_id=theme_id,
        tokens=ThemeTokens(**tokens_data),
        components=ThemeComponents(**components_data),
    )
