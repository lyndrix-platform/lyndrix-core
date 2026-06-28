#!/usr/bin/env python3
"""Check i18n key parity across locale files.

Convention: app/locales/{namespace}.{locale}.json

For every namespace, the English file is treated as the source of truth.
The script reports:
  - missing keys in non-English locales
  - extra keys in non-English locales that don't exist in English
  - placeholder mismatches (e.g. %{name} present in EN but missing in DE)

Exit code: 0 on success, 1 on parity issues.

Usage:
    python scripts/check_i18n_keys.py [--locales-dir app/locales]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Two placeholder dialects coexist (see app/core/i18n.py):
#   - NiceGUI core namespaces use %{name}
#   - i18next-facing namespaces (ui, settings) use {{name}} (+ _one/_other plurals)
PLACEHOLDER_RE = re.compile(r"%\{(\w+)\}")
I18NEXT_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)[^}]*\}\}")
SOURCE_LOCALE = "en"

try:
    # Reuse the canonical allowlist when importable without side effects.
    from core.i18n import I18NEXT_NAMESPACES  # type: ignore
except Exception:
    # Fallback — keep in sync with app/core/i18n.py:I18NEXT_NAMESPACES.
    I18NEXT_NAMESPACES = {"ui", "settings"}


def placeholder_re_for(namespace: str) -> re.Pattern[str]:
    """Select the placeholder dialect for *namespace*."""
    if namespace in I18NEXT_NAMESPACES:
        return I18NEXT_PLACEHOLDER_RE
    return PLACEHOLDER_RE


def flatten(data: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = str(v)
    return out


def load_files(locales_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    by_namespace: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for path in sorted(locales_dir.glob("*.*.json")):
        parts = path.stem.split(".")
        if len(parts) < 2:
            continue
        locale = parts[-1]
        namespace = ".".join(parts[:-1])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"ERROR: failed to parse {path}: {exc}", file=sys.stderr)
            sys.exit(2)
        by_namespace[namespace][locale] = flatten(data)
    return by_namespace


def placeholders(value: str, pattern: re.Pattern[str] = PLACEHOLDER_RE) -> set[str]:
    return set(pattern.findall(value))


def check(by_namespace: dict[str, dict[str, dict[str, str]]]) -> int:
    issues = 0
    for namespace, locales in sorted(by_namespace.items()):
        if SOURCE_LOCALE not in locales:
            print(f"ERROR: namespace '{namespace}' has no '{SOURCE_LOCALE}' source file")
            issues += 1
            continue
        src = locales[SOURCE_LOCALE]
        ph_re = placeholder_re_for(namespace)
        for locale, entries in sorted(locales.items()):
            if locale == SOURCE_LOCALE:
                continue
            missing = sorted(set(src) - set(entries))
            extra = sorted(set(entries) - set(src))
            ph_mismatch: list[str] = []
            for key, value in src.items():
                if key in entries and placeholders(value, ph_re) != placeholders(
                    entries[key], ph_re
                ):
                    ph_mismatch.append(key)
            if missing or extra or ph_mismatch:
                print(f"--- namespace '{namespace}' locale '{locale}' ---")
                for key in missing:
                    print(f"  missing: {key}")
                for key in extra:
                    print(f"  extra:   {key}")
                for key in ph_mismatch:
                    print(f"  placeholder mismatch: {key}")
                issues += len(missing) + len(extra) + len(ph_mismatch)
    if issues == 0:
        print("i18n parity: OK")
        return 0
    print(f"i18n parity: {issues} issue(s)")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_dir = Path(__file__).resolve().parents[1] / "app" / "locales"
    parser.add_argument(
        "--locales-dir",
        type=Path,
        default=default_dir,
        help=f"Path to locales directory (default: {default_dir})",
    )
    args = parser.parse_args()
    if not args.locales_dir.is_dir():
        print(f"ERROR: locales dir not found: {args.locales_dir}", file=sys.stderr)
        return 2
    return check(load_files(args.locales_dir))


if __name__ == "__main__":
    sys.exit(main())
