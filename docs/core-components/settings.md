# Settings Component

## Purpose

The Settings component provides the central UI for runtime configuration and operational settings changes.

## Main locations

- `app/core/components/settings/ui/routes.py`
- `app/core/components/settings/ui/settings_ui.py`

## Responsibilities

- register the settings route
- render platform configuration forms
- expose runtime-editable settings to operators
- provide a UI entry point for configuration workflows such as auth settings updates

## Events

The Settings route layer does not currently act as a major direct event publisher or subscriber on the global bus.

## UI endpoint

- `/settings`

## Runtime-editable settings

The System tab of the Settings UI edits a curated set of fields (`EDITABLE_SETTINGS` in `app/config.py`), grouped into **Application**, **Localization**, **Theming**, and **Plugins** categories. Values saved here are persisted to Vault (`lyndrix/core/settings`) and re-applied on boot via `Settings.hydrate_from_vault()` — **unless** the matching environment variable is set, in which case the env var always wins. This is what lets UI-saved settings survive a restart while keeping env vars authoritative.

## Theming

Lyndrix Core ships a dynamic theme engine (`app/core/theming/`, exported through `core.api`). Themes are data, not code.

### Two layers

Each theme lives under `app/assets/themes/<id>/` and has two files:

1. **Token layer** — `tokens.json`: semantic colour tokens with `light`/`dark` variants (`primary`, `secondary`, `accent`, `bg_body`, `text_muted`, …). Since 1.0 the **glass & border surfaces are colour tokens too** (`surface_glass`, `elevated_glass`, `glass_border`, `border_accent`, `border_soft`) — rgba values with alpha, so a theme can tone the glass effect down or off; the engine emits them as `--lx-surface-glass` / `--lx-elevated-glass` / `--lx-glass-border` / `--lx-border` / `--lx-border-soft`, consumed by both the React and NiceGUI UIs.
2. **Component layer** — `components.json`: maps every `UIStyles` class-attribute name to a Tailwind class string.

The theme validator (`core/theming/schema.py`) backfills any missing keys with `UIStyles` defaults, so **partial themes are valid**.

### Runtime model

- `get_theme_engine()` returns the singleton `ThemeEngine`.
- `resolve_component_styles(theme_id=None, plugin_id=None)` returns the merged style map (active theme + optional plugin overrides).
- `resolve_runtime_palette(dark)` returns the Quasar/NiceGUI colour palette for the active theme.
- `list_available_themes()` enumerates theme IDs on disk.
- `ThemePack`, `ThemeTokens`, `ThemeComponents`, and `load_theme_pack` are the data models, also exported from `core.api`.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `THEME_ENGINE_ENABLED` | `True` | Enable the dynamic theme engine. |
| `THEME_DB_OVERRIDES_ENABLED` | `False` | Allow theme overrides stored in the DB to take effect. |
| `DEFAULT_THEME_ID` | `default` | Theme applied when no per-user selection exists. |

All three are also editable from the Settings UI (Theming category).

### Using theme styles in plugins

Always go through `core.api` — never import from `ui.theme` directly:

```python
from core.api import UIStyles

ui.card().classes(UIStyles.CARD_BASE)
ui.label("Title").classes(UIStyles.TITLE_H2)
ui.label("Hint").classes(UIStyles.TEXT_MUTED)
```

A plugin can override specific style keys for its own pages without touching the global theme:

```python
def setup(ctx):
    ctx.register_theme_overrides({
        "CARD_BASE": "p-4 rounded-xl border border-zinc-700 bg-zinc-900",
    })
```

Overrides are in-memory and apply only when that plugin's pages resolve styles with their own `plugin_id`.

### Creating a custom theme

1. Copy `app/assets/themes/default/tokens.json` to `app/assets/themes/<your-id>/tokens.json` and change the colours.
2. Copy `components.json` and adjust Tailwind strings (omit any keys you want defaulted — they are backfilled).
3. Select the theme in **Settings → Appearance**, or set `DEFAULT_THEME_ID=<your-id>`.

Alternatively, upload a ZIP containing both JSON files via **Settings → Appearance → Upload Theme**.

## Typography and font override

The Settings UI (and global app shell) typography is configured in [app/ui/theme.py](../../app/ui/theme.py) via CSS variables inside `:root`:

- `--lx-font-sans`
- `--lx-font-mono`

Current strategy:

- prefer Ubuntu system sans for UI text
- use JetBrains Mono for code/mono text

If a browser appears to replace fonts unexpectedly, verify both the browser-computed font and Linux font fallback.

### Browser check (DevTools Console)

```js
(() => {
	const picks = ['body', 'h1', 'h2', 'p', 'button', 'input', '.q-btn', '.q-card'];
	for (const sel of picks) {
		const el = document.querySelector(sel);
		if (!el) continue;
		console.log(`${sel}: ${getComputedStyle(el).fontFamily}`);
	}
	for (const f of document.fonts) {
		console.log(`${f.family} | status=${f.status} | weight=${f.weight} | style=${f.style}`);
	}
})();
```

### Ubuntu fontconfig fallback check

```bash
fc-match -s "Ubuntu" | head -n 10
fc-match -s "JetBrains Mono" | head -n 10
fc-match -s "sans-serif" | head -n 10
```

Notes:

- run the JavaScript snippet in browser DevTools, not in bash
- if fonts still look stale after changes, hard reload (`Ctrl+Shift+R`)
