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
- `plugin:files_changed`
- `ui:needs_refresh`

## Runtime notes

- plugins remain pending until the database-backed activation state can be restored
- `requirements.txt` without a matching `vendor/` directory is treated as a warning condition
- repository names with dashes are normalized to underscores for Python imports
- plugin secret access is scoped through `ModuleContext`
