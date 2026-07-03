# Lyndrix Theming v2 — "Everything is a token" Design & Delivery Plan

## Vision (from the user)

Make both UIs (NiceGUI **and** React) as customizable as humanly possible — every
CSS value (colours, **corner radius**, spacing, typography, shadows, blur, gradients,
transitions) changeable through a theme file, Home-Assistant-style but deeper. The
**layout itself** (header, navbar/sidebar, content) must be theme-configurable, and
header/navbar must be **hidable**. All of it overridable at **three scopes** with a
clear precedence, and the effective config cached in the browser so the correct
theme/language paints instantly — even while logged out.

Everything is built on the **file-based theme packs we already have**; a live GUI
editor and remote theme repositories come later, layered on top.

---

## Current state (verified by exploration — do not rebuild these)

- **One engine is already the single token source.** `lyndrix-core/app/core/theming/engine.py`
  → `resolve_css_variables(dark, theme_id)` emits `--lx-*` CSS variables consumed by
  **both** UIs: NiceGUI server-side (`app/ui/theme.py`) and React via
  `GET /api/themes/{id}/css-vars` (`lyndrix-ui/src/theme/ThemeProvider.tsx`).
- **React is already token-driven** for colour: Tailwind maps `colors.lx.*`,
  `borderRadius.*`, `boxShadow.glow` → `var(--lx-*)`; stale-while-revalidate cache in
  `localStorage`; per-user overrides + background images already work. 0 hardcoded
  palette utilities in `src/`.
- **Theme packs** are `tokens.json` + `components.json` (+ `assets/`) on disk at
  `app/assets/themes/{id}/`, ZIP-uploadable with path-safety.
- **React theme selection is already independent** of the NiceGUI active theme
  (`lyndrix_react_theme` vs server `DEFAULT_THEME_ID`) — the "separate per-UI" capability
  partly exists already.

### Gaps to close
1. Token model is **colours-only** (13 semantic). No first-class radius/spacing/
   typography/shadow/blur/transition/gradient. `--lx-radius-*` is emitted but **dead**
   (hardcoded 6/12/20, nothing consumes it).
2. **Literal walls**: `theme.py`'s big `<style>` block and `components.json` bake hex,
   `slate/zinc`, `p-6`, `rounded-2xl`, `from-cyan-400` — a token change can't touch them.
   React has a few inline literal bypasses (e.g. `FeatureCard.tsx` `20px`).
3. **Layout is outside theming**: header height, sidebar width, content `max-w-7xl`,
   modal insets are literals in `app/ui/layout.py` and `lyndrix-ui/src/components/layout/AppShell.tsx`.
4. **No hide-header/navbar mechanism**; nav sourced from manifests (NiceGUI
   `get_nav_items()`) / `/api/plugins` (React `Sidebar.tsx`).
5. **No generic per-user preferences API** — only `/api/me/background` exists (bespoke).
6. **Validation is presence-only** (no value checks; warnings swallowed).
7. **NiceGUI mutates a process-global `UIStyles`** (`theme.py:398-405`) — the A4 race;
   must become per-request/per-document to support per-user/browser overrides.

---

## Target architecture

### 1. Token contract = the universal API (CSS variables)
Everything themeable is a `--lx-*` variable the engine emits; **both UIs read the same
set**. This is per-document, which is what makes per-user/per-browser overrides and the
A4 fix fall out naturally. **Strategy: map the Tailwind scale → vars** (chosen) so
existing utilities (`rounded-md`, `p-4`, `shadow`, `gap-3`) auto-resolve — minimal
component churn — plus targeted replacement of hard literals in the `<style>` wall and
`components.json`.

New token categories (Phase 1): `radius`, `spacing`, `typography` (family/size/weight/
leading/tracking), `shadow`, `blur`, `transition`, `gradient`, `border`. Layout tokens
(Phase 2): `--lx-header-h`, `--lx-sidebar-w`, `--lx-sidebar-w-collapsed`,
`--lx-content-max-w`, `--lx-content-pad`, `--lx-modal-inset`, `--lx-menu-w`.

### 2. Modular targeting — one theme for both, or separate per UI
Theme selection carries a **target**: `both` | `react` | `nicegui`. A selection is
`{ react: <themeId>, nicegui: <themeId> }` (equal when "both"). Keep the two resolvable
independently (already partly true). "Do it the modular, Lyndrix way."

### 3. Layered resolution (the heart of it)
Single precedence chain, applied to theme selection, language, layout config, visibility
toggles, and individual token overrides:

```
effective(x) = browserOverride(x) ?? userPreference(x) ?? systemDefault(x) ?? themeDefault(x)
```

- **themeDefault** — the active theme pack's value. **This is the default for everything**
  ("default set to by theme").
- **systemDefault** — global, set in System Settings (e.g. default theme per target,
  default language, global layout/visibility). Model it like `DEFAULT_THEME_ID` today.
- **userPreference** — per-user, **cross-device**, server-persisted. If a user pins a
  theme/language/layout in their profile, they get it on any browser, logged in.
- **browserOverride** — per-browser. A user can **"pin this browser"** and set
  theme/language/layout **only for this browser**; when pinned, browser wins.

### 4. Browser-storage cache for instant + logged-out first paint
The **effective resolved config** (theme id(s) + their css-var payloads, language,
layout/visibility) is always mirrored into browser storage (`localStorage` on React;
cookie / `app.storage.browser` on NiceGUI). On load — **even logged out** — the client
reads browser storage first and paints the correct theme/language immediately, then
revalidates against the server when authenticated. Works for browser-pinned,
profile-set, and system-set themes alike.

### 5. New generic per-user preferences API
`GET/PUT /api/me/preferences` — a namespaced key/value store (theme selection per target,
language, layout overrides, visibility toggles, token overrides). Replaces the need for
bespoke endpoints; `/api/me/background` can migrate onto it later. Consumed by both UIs.

### 6. Schema + validation upgrade
Theme-pack schema v2 covering the new token categories + `layout` + a `targets` field.
**Value-level validation** (valid CSS colour / length / number ranges) that actually
fails or quarantines bad packs, not presence-only warnings. Backward compatible: a v1
colours-only pack still loads (missing categories fall back to built-in defaults).

### 7. Resolve A4 as part of this
NiceGUI must stop mutating the process-global `UIStyles` and instead inject a per-request
`<style>` of resolved `--lx-*` vars (React already does this). This is required anyway
for per-user/per-browser overrides to work per session.

### 8. Built for what comes next
- **Live editor (next step):** the override write-path and theme-token data model are
  designed so a GUI editor can either (a) edit a theme pack, or (b) write user/browser
  overrides — same schema, same endpoints. Editor ships in a later phase.
- **Remote theme repos (much later):** theme packs become installable from git repos like
  plugins, all on top of the file-based pack format.

---

## Phased delivery (each phase = a handoff unit)

> Model tiering guidance is per phase. Design/schema/precedence = Opus. Mechanical
> refactors (Tailwind mapping, literal replacement, UI wiring) = Sonnet. Small
> mechanical passes = Haiku acceptable.

### Phase 1 — Design-token contract (FIRST, chosen)
**Goal:** radius/spacing/typography/shadow/blur/transition/gradient/border become real,
theme-driven tokens on both UIs; kill the literal walls. Keep layout/visibility in mind
but don't build them yet.

- **Core engine** (`engine.py`, `models.py`, `schema.py`): add token categories +
  emit `--lx-*` for each; make `--lx-radius-*` real; keep v1 packs valid via fallbacks.
  *(Opus: design the token taxonomy + schema; Sonnet: implement emission.)*
- **Kill NiceGUI literals** (`app/ui/theme.py` `<style>` block + `components.json`):
  replace hardcoded hex/radii/spacing/shadows/gradients/fonts with `var(--lx-*)`;
  unify the engine-on/off variable sets. *(Sonnet, mechanical but careful.)*
- **React Tailwind mapping** (`lyndrix-ui/tailwind.config.js`, `src/index.css`): map
  `borderRadius`/`spacing`/`boxShadow`/`fontFamily`/`fontSize` scales → `--lx-*`; fix
  inline literal bypasses (`FeatureCard.tsx`, etc.). *(Sonnet.)*
- **Default theme pack** (`app/assets/themes/default/tokens.json`): populate the new
  token categories with the current values (so nothing changes visually at first).
- **Verify:** change a single radius/spacing token in the pack → both UIs re-render with
  new corners/spacing; screenshot via `run-lyndrix-core` skill; React visual check.

### Phase 2 — Layout tokens + layered resolution + per-user/browser prefs + hide nav
**Goal:** layout dimensions themeable; header/navbar hidable at all three scopes; the
precedence chain + browser-storage cache live.

- **Per-user preferences API** (`/api/me/preferences`) — server foundation. *(Opus/Sonnet.)*
- **Resolution engine**: implement `effective(x)` precedence + the browser-storage cache
  contract (logged-out first paint). *(Opus for the model; Sonnet to wire.)*
- **Layout tokens** in the pack + emit `--lx-header-h`/`--lx-sidebar-w`/… ; consume them
  in `app/ui/layout.py` and `AppShell.tsx`/`Header.tsx`/`Sidebar.tsx`.
- **Hide header/navbar**: conditional render gated by resolved visibility (theme default,
  user/browser override). NiceGUI: gate `ui.header`/`ui.left_drawer`, relocate the
  bell/profile controls if header hidden. React: conditional render (reactive, no reload).
- **A4 fix**: NiceGUI per-request var injection instead of global `UIStyles` mutation.
- **Verify:** toggle hide-header per browser (persists in browser storage, logged-out
  survives); set a profile theme → appears on a second browser; system default respected.

### Phase 3 — Settings UI surfaces
- **System Settings**: global default theme (per target react/nicegui/both) + default
  language + global layout/visibility defaults.
- **Profile Settings**: account-global theme/language/layout (cross-device).
- **New "This Browser" section** (under user settings): "pin this browser" + per-browser
  theme/language/layout overrides; clearly shows the effective source (browser > user >
  system > theme). *(Sonnet.)*

### Phase 4 — Live editor
Live GUI editor with color pickers + radius/spacing sliders + layout controls and live
preview, writing either a theme pack or user/browser overrides through the Phase-2 data
model. *(Sonnet build, Opus review.)*

### Phase 5 — Remote theme repositories (much later)
Install theme packs from git repos like plugins, on top of the file-based format.

---

## Confirmed decisions
- "Default UI for the entire application" = **default theme per UI target** (react/nicegui). ✓
- Token→component strategy = **map Tailwind scale → vars** (min churn), and the system must
  support applying one theme to **both** UIs at once OR a **separate** theme per UI. ✓
- Scopes = **all three** (browser + user + global), precedence
  browser > user > system > theme, **default = by theme**. ✓
- Effective config **cached to browser storage** for instant + logged-out first paint. ✓
- Phase 1 = **design tokens first**. ✓  Live editor = **later phase**, but design the
  override write-path/data-model so the editor can drive it. ✓  Remote theme repos = later. ✓
- **NiceGUI = full parity** with React: Phase 1 kills *every* literal in the `<style>` wall
  and `components.json` (auth radials, SSO buttons, scrollbar, glass, gradients, fonts),
  not just the common ones. Both UIs are equally theme-driven while they coexist. ✓

## Phase 2 also absorbs
- **A4** (NiceGUI global `UIStyles` mutation → per-request var injection).
- **Nav hardening** (React-migration class of bug, surfaced by Meeting Bingo): the NiceGUI
  `get_nav_items()` must skip a plugin whose `ui_route` has no registered NiceGUI page
  (react-only plugins). Immediate case fixed in the bingo manifest; make core robust here.

## Still to pin before Phase-2 handoff (not blocking Phase 1)
- Exact spacing/radius scale granularity (full Tailwind scale vs curated subset) — Phase 1.
- Where precedence resolution runs (server computes user+system+theme "effective config";
  client applies browser overrides + caches) — Phase 2.
- `/api/me/preferences` payload schema + "pin this browser" exact semantics — Phase 2.

## Cross-cutting constraints (from repo conventions)
- Docs German / code English; generated-not-hand-written (fix upstream tokens, not
  artifacts); API-first; never block the event loop; both UIs live only via the API.
- `docker/.env.dev` has CRLF — pipe `| tr -d '\r'`.
- Verify with the `run-lyndrix-core` skill (screenshots) + React dev build.
