from __future__ import annotations

import json
import logging
from pathlib import Path

from core.theming.models import ThemeComponents, ThemePack, ThemeTokens

log = logging.getLogger("Core:ThemeLoader")


def load_theme_pack(base_dir: Path, theme_id: str) -> ThemePack:
    theme_dir = base_dir / theme_id
    tokens_path = theme_dir / "tokens.json"
    components_path = theme_dir / "components.json"

    with tokens_path.open("r", encoding="utf-8") as f:
        tokens_data = json.load(f)

    with components_path.open("r", encoding="utf-8") as f:
        components_data = json.load(f)

    pack = ThemePack(
        theme_id=theme_id,
        tokens=ThemeTokens(**tokens_data),
        components=ThemeComponents(**components_data),
    )

    # Validate and backfill missing keys with UIStyles defaults.
    try:
        from core.theming.schema import validate_theme_pack
        for warning in validate_theme_pack(pack):
            log.warning(warning)
    except Exception:
        pass  # Validation is best-effort; never block theme loading.

    return pack
