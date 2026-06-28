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

import copy
import hashlib
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

# Parallel NESTED store for HTTP catalog serving (i18next-shaped clients):
#   {locale: {namespace: <nested dict>}}
# This keeps the original tree intact (no flattening) so it can be served
# straight to i18next's ``addResourceBundle``.
_nested: dict[str, dict[str, Any]] = {}

# Catalog content fingerprint (first 12 hex chars of a sha256 over every
# locale file's path/mtime/size).  Used for HTTP ETag / version negotiation.
# Bumps automatically when plugins register new locale directories.
_catalog_version: str | None = None

# Namespaces served to i18next-shaped (React/external) clients.  These use the
# ``{{name}}`` placeholder dialect + ``_one``/``_other`` plurals — never the
# NiceGUI ``%{name}`` dialect — so they are kept on an explicit allowlist that
# the HTTP catalog endpoint serves by default.
I18NEXT_NAMESPACES: set[str] = {"ui", "settings"}

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


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *src* into *dst* in place and return *dst*.

    Nested dicts are merged key-by-key; any non-dict value (or a type change)
    overwrites the destination.
    """
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


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
            # Also keep the original nested tree for HTTP catalog serving.
            ns_root = _nested.setdefault(locale, {}).setdefault(namespace, {})
            _deep_merge(ns_root, data)
            log.debug(f"i18n: loaded {path.name} ({len(flat)} keys)")
        except Exception as exc:
            log.warning(f"i18n: failed to load {path}: {exc}")
    _recompute_version()


def _recompute_version() -> None:
    """Recompute the catalog content fingerprint over all registered dirs.

    Hashes the sorted ``(path, mtime_ns, size)`` triples of every locale file
    so the version is stable across processes and bumps whenever a file is
    added/changed (e.g. a plugin registering its ``locales/`` directory).
    """
    global _catalog_version
    items: list[tuple[str, int, int]] = []
    for dir_key in _registered_dirs:
        directory = Path(dir_key)
        if not directory.is_dir():
            continue
        for path in directory.glob("*.*.json"):
            try:
                st = path.stat()
            except OSError:
                continue
            items.append((str(path), st.st_mtime_ns, st.st_size))
    items.sort()
    h = hashlib.sha256()
    for path_str, mtime_ns, size in items:
        h.update(f"{path_str}|{mtime_ns}|{size}\n".encode("utf-8"))
    _catalog_version = h.hexdigest()[:12]


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


# ------------------------------------------------------------------
# HTTP catalog API (nested, i18next-shaped) — additive; does not touch t()
# ------------------------------------------------------------------


def client_namespaces() -> list[str]:
    """Return the sorted allowlist of namespaces served to i18next clients."""
    return sorted(I18NEXT_NAMESPACES)


def register_client_namespace(ns: str) -> None:
    """Add *ns* to the i18next client allowlist and bump the catalog version.

    Plugins call this (indirectly, via the ``i18n_namespace`` manifest field)
    to opt one of their already-registered ``locales/<ns>.<locale>.json``
    namespaces into the HTTP catalog the React UI consumes.  The namespace
    files must use the i18next ``{{name}}`` placeholder dialect (and
    ``_one``/``_other`` plurals) — never the NiceGUI ``%{name}`` dialect.

    Idempotent: re-registering an existing namespace is a no-op (the version
    recompute is harmless).  ``_recompute_version()`` runs *after* the set
    mutation so clients revalidating with ``?v=`` see the version change and
    pick up the new namespace instead of getting a stale 304.
    """
    if not ns:
        return

    # Cheap dialect sanity check: warn if a served file for this namespace
    # uses the NiceGUI ``%{...}`` placeholder dialect (client namespaces must
    # use i18next ``{{ }}``).  Best-effort only — never blocks registration.
    try:
        for loc_tree in _nested.values():
            ns_tree = loc_tree.get(ns)
            if ns_tree and _PLACEHOLDER_RE.search(json.dumps(ns_tree)):
                log.warning(
                    f"i18n: client namespace '{ns}' contains NiceGUI '%{{}}' "
                    f"placeholders; client namespaces must use i18next '{{{{ }}}}'"
                )
                break
    except Exception:
        pass

    I18NEXT_NAMESPACES.add(ns)
    # Order matters: recompute AFTER mutating the set so the version reflects
    # the newly-allowed namespace.
    _recompute_version()


def get_catalog_version() -> str:
    """Return the current catalog content fingerprint (recomputed if needed)."""
    if _catalog_version is None:
        _recompute_version()
    return _catalog_version or ""


def get_catalog(locale: str, namespaces: list[str] | None = None) -> dict[str, Any]:
    """Return the nested ``{namespace: {...}}`` catalog for *locale*.

    Each namespace is deep-merged OVER the English nested tree, so a partial
    non-English locale falls back to EN at serve time — mirroring ``t()``'s
    English fallback.  Restricted to *namespaces* when given.
    """
    en_tree = _nested.get("en", {})
    loc_tree = _nested.get(locale, {})
    ns_keys = set(namespaces) if namespaces is not None else (
        set(en_tree) | set(loc_tree)
    )
    out: dict[str, Any] = {}
    for ns in ns_keys:
        # Deep-copy both trees: _deep_merge aliases nested dicts by reference,
        # so merging directly over `en_tree` would mutate the shared English
        # store in place (poisoning EN with the requested locale's strings).
        merged: dict[str, Any] = copy.deepcopy(en_tree.get(ns, {}))
        if locale != "en":
            _deep_merge(merged, copy.deepcopy(loc_tree.get(ns, {})))
        out[ns] = merged
    return out


def available_locales() -> list[str]:
    """Return locales present in the nested store, limited to supported ones."""
    return sorted(set(_nested) & _get_supported())


def reload_core_locales() -> None:
    """Force a re-scan of the core locales directory.

    Useful when new locale files are added while the process is running
    (e.g. a new core component ships its own ``{namespace}.{locale}.json``).
    Safe to call multiple times — only new/changed files are added.
    """
    _registered_dirs.discard(str(_LOCALES_DIR))
    _load_dir(_LOCALES_DIR)


def register_plugin_locales(plugin_path: Path) -> None:
    """Scan *plugin_path*/locales/ and load any JSON locale files found.

    Convention: ``{plugin_path}/locales/{plugin_name}.{locale}.json``

    Called automatically by the ModuleManager — plugin authors only need
    to create the files.
    """
    _load_dir(plugin_path / "locales")
