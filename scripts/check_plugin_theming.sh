#!/usr/bin/env bash
# Theming convention guard for Lyndrix plugin UIs (Theming v2).
#
# CONVENTION: plugin UI code MUST style itself through the theming engine —
# `--lx-*` CSS variables, the shared `.lx-*` classes, or `UIStyles` — so it
# responds to the active theme and light/dark. The following are DISALLOWED in
# a plugin's UI source (app/ui/**, src/ui/**):
#   - raw Tailwind palette utilities (bg/text/border/from/via/to/ring-<hue>-<n>)
#   - raw white/black utilities incl. scrims (bg-white, bg-black/40 — use
#     var(--lx-scrim) / var(--lx-on-accent-text) instead)
#   - inline hex colours in style props
#   - hardcoded `fontFamily: 'monospace'` (use var(--lx-font-mono) / .lx-mono)
#   - hand-rolled multi-stop gradients (use --lx-gradient-* / .lx-card-accent)
#   - the invalid `var(--lx-*)<hexsuffix>` pattern (use color-mix())
#
# Usage:  scripts/check_plugin_theming.sh <plugin-repo-dir> [<plugin-repo-dir> ...]
# Exit non-zero if any violation is found. Prints file:line + the offending text.
set -uo pipefail

PALETTE='(bg|text|border|from|via|to|ring|fill|stroke|divide|ring-offset)-(zinc|slate|gray|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-[0-9]{2,3}'
MONOCHROME='(bg|text|border)-(white|black)(/[0-9]{1,3})?([^a-zA-Z0-9-]|$)'
INLINE_HEX="#[0-9a-fA-F]{3,8}\b"
MONO="fontFamily:[[:space:]]*['\"]monospace"
GRAD='(from-[a-z]+-[0-9]+[[:space:]]+via-|bg-gradient-to-[a-z]+[^\"'\'' ]*from-)'
BADSUFFIX='var\(--lx-[a-z0-9-]+\)[0-9a-fA-F]{2}'

total=0
for repo in "$@"; do
  name="$(basename "$repo")"
  dirs=()
  [ -d "$repo/app/ui" ] && dirs+=("$repo/app/ui")
  [ -d "$repo/src/ui" ] && dirs+=("$repo/src/ui")
  [ ${#dirs[@]} -eq 0 ] && continue
  # Only hand-authored source; skip built bundles / vendored / minified.
  files=$(find "${dirs[@]}" -type f \( -name '*.py' -o -name '*.tsx' -o -name '*.ts' -o -name '*.css' \) \
            2>/dev/null | grep -vE 'ui_bundle\.js|\.min\.|/vendor/|/node_modules/|/dist/|/static/')
  [ -z "$files" ] && continue
  hits=$(echo "$files" | xargs grep -HnE "$PALETTE|$MONOCHROME|$INLINE_HEX|$MONO|$GRAD|$BADSUFFIX" 2>/dev/null \
           | grep -vE '^\s*#|//|/\*|--lx-')   # drop comment lines + token defs
  if [ -n "$hits" ]; then
    n=$(echo "$hits" | wc -l | tr -d ' ')
    total=$((total + n))
    echo "── $name: $n violation(s) ──"
    echo "$hits"
    echo
  else
    echo "✓ $name: clean"
  fi
done

echo "TOTAL violations: $total"
[ "$total" -eq 0 ]
