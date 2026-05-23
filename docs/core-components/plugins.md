# Plugins Component

## Purpose

The Plugins component provides module discovery, plugin lifecycle management, marketplace integration, and installation workflows.

## Main locations

- `app/core/components/plugins/entrypoint.py`
- `app/core/components/plugins/logic/manager.py`
- `app/core/components/plugins/logic/plugin_service.py`
- `app/core/components/plugins/logic/context.py`
- `app/core/components/plugins/ui/*`

## Responsibilities

- Discover and load core modules + user plugins
- Enforce manifest validation and dependency order
- Persist plugin state in DB
- Install/upgrade/uninstall plugins from GitHub
- Request and track plugin collection sync via Git component
- Provide plugin runtime sandbox (`ModuleContext`)

## Events

### Subscribes

- `git:status_update`
- `system:boot_complete`
- `plugin:files_changed`
- `db:connected` (fallback activation path)

### Emits

- `git:sync`
- `plugin:install_started`
- `plugin:installed`
- `plugin:install_failed`
- `plugin:files_changed`
- `ui:needs_refresh`
