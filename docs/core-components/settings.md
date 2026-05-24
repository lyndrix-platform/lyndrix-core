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
