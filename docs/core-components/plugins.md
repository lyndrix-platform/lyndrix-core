# Plugins Component

## Purpose

The Plugins component provides discovery, lifecycle management, marketplace integration, and installation workflows for both built-in modules and user-installed plugins.

## Main locations

- `app/core/components/plugins/entrypoint.py`
- `app/core/components/plugins/logic/manager.py`
- `app/core/components/plugins/logic/plugin_service.py`
- `app/core/components/plugins/logic/context.py`
- `app/core/components/plugins/ui/*`

## Responsibilities

- discover core components and user plugins from disk
- import and validate plugin entrypoints and manifests
- create isolated `ModuleContext` instances
- persist plugin activation state in the database
- install, upgrade, reload, disable, and uninstall plugins
- integrate with GitHub archives and version tags
- request marketplace collection sync through the Git component
- trigger UI refreshes after lifecycle changes

## Internal layers

### ModuleManager

Owns runtime loading behavior:

- scans `core/components` and `plugins`
- validates folder names and manifests
- restores activation state from `plugin_states`
- blocks plugins with unmet dependencies
- activates plugins once the database-backed state is known

### PluginService

Owns package acquisition behavior:

- downloads plugin source from GitHub
- performs safe ZIP extraction into staging
- installs plugin dependencies into `vendor/`
- swaps upgraded plugin directories atomically
- maintains marketplace metadata and collection caches
- runs a background **update checker** that compares installed versions to release tags

## Background update checker (1.0)

A periodic task (interval `LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S`, default 6h,
plus one check shortly after boot) compares each installed plugin's version to
the newest release tag from its repo (reusing the cached tags API — no extra
network cost per request). Results live in memory and surface as
`PluginOut.latest_version` / `PluginOut.update_available` on `GET /api/plugins`,
so the React plugin card shows an **update badge**. A newer tag emits
`plugin:update_available` (forwarded over SSE) and a persisted, admin-gated bell
entry. The marketplace list itself loads lazily and negative-caches failures, so
opening the plugin manager never blocks on a slow collection fetch.

## Logs

Per-module log lines land in a thread-safe, per-source in-memory ring buffer
(`core/logger.py`, `LogRingBuffer`). `GET /api/logs?source=Plugin:<name>&limit&level`
(and `/api/logs/sources`, both `api:read`) serve them; `PluginOut.log_source`
precomputes the `{Core|Plugin}:<name>` source string. The React plugin manager
opens a live log modal for **any** module — plugins and core components alike.

## Events

### Subscribes

- `git:status_update`
- `system:boot_complete`
- `plugin:files_changed`
- `db:connected`

### Emits

- `git:sync`
- `plugin:install_started`
- `plugin:installed`
- `plugin:install_failed`
- `plugin:update_available`
- `plugin:state_changed`
- `plugin:files_changed`
- `ui:needs_refresh`

## Runtime notes

- plugins remain pending until the database-backed activation state can be restored
- `requirements.txt` without a matching `vendor/` directory is treated as a warning condition
- repository names with dashes are normalized to underscores for Python imports
- plugin secret access is scoped through `ModuleContext`
- `LYNDRIX_PLUGINS_DESIRED` (comma-separated `url[@version]`) is reconcilable from a marketplace-fed picker in **Settings → Plugins**, which writes the same string
