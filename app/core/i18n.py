"""Lyndrix i18n module — zero-dependency JSON loader.

Locale files live at:

    Core:    app/locales/{namespace}.{locale}.json
    Plugins: plugins/{name}/locales/{name}.{locale}.json

Usage:

    from core.i18n import t, set_locale, get_locale

    ui.label(t('core.nav.dashboard'))   # current user's locale, falls back to en
    ui.label(t('plugins.tabs.installed'))
    ui.label(t('my_plugin.welcome', name='World'))  # %{name} placeholder

The ModuleManager calls register_plugin_locales(plugin_path) automatically
for every plugin it loads — plugin authors only need to create the files.

Migration note
--------------
If the project ever grows to 10+ languages with external translators, swap
this module for Babel/gettext.  The public API (t / get_locale / set_locale /
register_plugin_locales) is intentionally kept compatible so the call-sites
don't need to change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.logger import get_logger
from core.session import get_user_value, set_user_value

log = get_logger("Core:i18n")

# ------------------------------------------------------------------
# Internal catalogue:  {locale: {dotted.key: "translated string"}}
# ------------------------------------------------------------------
_catalogue: dict[str, dict[str, str]] = {}

# Directories that have been scanned already (avoid double-loading).
_registered_dirs: set[str] = set()

# Deferred config cache.
_supported: set[str] | None = None
_default: str = "en"

# Placeholder pattern: %{name}
_PLACEHOLDER_RE = re.compile(r"%\{(\w+)\}")


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _flatten(data: Any, prefix: str = "") -> dict[str, str]:
    """Recursively flatten a nested dict into dotted keys."""
    out: dict[str, str] = {}
    for k, v in data.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full_key))
        else:
            out[full_key] = str(v)
    return out


def _load_dir(directory: Path) -> None:
    """Load all ``{namespace}.{locale}.json`` files from *directory*."""
    key = str(directory)
    if key in _registered_dirs or not directory.is_dir():
        return
    _registered_dirs.add(key)
    for path in directory.glob("*.*.json"):
        parts = path.stem.split(".")  # e.g. ["core", "en"]
        if len(parts) < 2:
            continue
        locale = parts[-1]
        namespace = ".".join(parts[:-1])  # e.g. "core" or "my.plugin"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            flat = _flatten(data, namespace)
            _catalogue.setdefault(locale, {}).update(flat)
            log.debug(f"i18n: loaded {path.name} ({len(flat)} keys)")
        except Exception as exc:
            log.warning(f"i18n: failed to load {path}: {exc}")


# Load core locale files immediately.
_LOCALES_DIR = Path(__file__).parent.parent / "locales"
_load_dir(_LOCALES_DIR)


def _get_supported() -> set[str]:
    global _supported, _default
    if _supported is None:
        try:
            from config import settings  # noqa: PLC0415

            _default = settings.DEFAULT_LOCALE
            _supported = {
                s.strip() for s in settings.SUPPORTED_LOCALES.split(",") if s.strip()
            }
        except Exception:
            _supported = {"en", "de"}
    return _supported


def _current_locale() -> str:
    """Return the locale for the active NiceGUI request, or the configured default."""
    try:
        locale = get_user_value("locale", _default)
        return locale if locale in _get_supported() else _default
    except Exception:
        return _default


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def t(key: str, locale: str | None = None, **kwargs) -> str:
    """Return the translation of *key* for *locale* (default: current user's locale).

    Placeholders use ``%{name}`` syntax — pass them as keyword arguments::

        t('core.common.by', name='Alice')  # "by Alice"

    Falls back to English if the key is missing in the requested locale.
    Returns the bare key string if no translation exists anywhere, so the
    UI never crashes on an untranslated string.
    """
    if locale is None:
        locale = _current_locale()

    # Try requested locale, then English fallback.
    for loc in (locale, "en"):
        value = _catalogue.get(loc, {}).get(key)
        if value is not None:
            if kwargs:
                value = _PLACEHOLDER_RE.sub(
                    lambda m: str(kwargs.get(m.group(1), m.group(0))), value
                )
            return value

    log.debug(f"i18n: missing key '{key}' (locale={locale})")
    return key


def get_locale() -> str:
    """Return the current user's locale string (e.g. ``'en'``, ``'de'``)."""
    return _current_locale()


def set_locale(locale: str) -> None:
    """Persist *locale* in the current user's NiceGUI session storage."""
    if locale not in _get_supported():
        log.warning(f"i18n: unsupported locale '{locale}', ignoring")
        return
    set_user_value("locale", locale)


def register_plugin_locales(plugin_path: Path) -> None:
    """Scan *plugin_path*/locales/ and load any JSON locale files found.

    Convention: ``{plugin_path}/locales/{plugin_name}.{locale}.json``

    Called automatically by the ModuleManager — plugin authors only need
    to create the files.
    """
    _load_dir(plugin_path / "locales")
