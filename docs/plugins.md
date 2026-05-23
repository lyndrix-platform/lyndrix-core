# Plugin Development Guide

This guide explains how to build plugins for Lyndrix Core and how the core runtime interacts with them.

## Plugin model

A Lyndrix plugin is a Python package inside `/app/plugins/<plugin_folder>` with an `entrypoint.py` module.

At minimum, a plugin provides:

- a `manifest`
- a `setup(ctx)` function

The manifest tells Lyndrix what the plugin is, what permissions it needs, and how it should behave during activation.

## Directory structure

A typical plugin looks like this:

```text
app/plugins/my_plugin/
├── entrypoint.py
├── requirements.txt        # optional
├── vendor/                 # generated during install, optional in source repos
├── assets/                 # optional static assets
└── locales/                # optional translation files, auto-registered
```

Rules and conventions:

- the folder name must be a valid Python identifier
- if a repository name contains `-`, Lyndrix normalizes it to `_` for imports
- `requirements.txt` is supported, but dependencies are installed into the plugin-local `vendor/` directory

## Stable plugin API

Plugin code should import from `core.api` rather than from internal core modules.

```python
from core.api import __api_version__, ModuleManifest
```

Current stable API version:

- `__api_version__ = "1.0.0"`

The goal is to give plugin authors a stable surface even when the internal core structure evolves.

## Manifest fields

`ModuleManifest` supports the following important fields:

- `id` — required unique ID, typically `lyndrix.plugin.<name>`
- `name` — display name
- `version` — plugin version string
- `description`, `author`, `icon` — metadata for UI and marketplace views
- `type` — use `PLUGIN` for normal plugins
- `ui_route` — route mounted by the plugin in the UI
- `permissions.subscribe` — topics the plugin may subscribe to
- `permissions.emit` — topics the plugin may emit
- `permissions.vault_paths` — additional Vault paths if needed
- `settings_schema` — optional plugin-defined settings metadata
- `dependencies` — list of required modules or plugins
- `min_core_version` — minimum supported Lyndrix API/core version
- `auto_enable_on_install` — whether the plugin should activate automatically
- `repo_url` — source repository URL used for updates and marketplace metadata

## Lifecycle hooks

### Required hook

- `setup(ctx)`

`setup(ctx)` can be synchronous or asynchronous. Lyndrix calls it when the plugin becomes active.

### Optional hooks

- `teardown(ctx)` — cleanup during deactivation or unload
- `render_settings_ui(ctx)` — render plugin settings inside the platform settings UI
- `render_dashboard_widget(ctx)` — render dashboard widgets on the main dashboard

## Runtime states

Plugins can move through several internal states:

- `initializing`
- `active`
- `disabled`
- `blocked`

`blocked` is used when the plugin is enabled but a declared dependency is not available or not active yet.

## ModuleContext

Each plugin receives a `ModuleContext` instance named `ctx`. This is the supported bridge into the core runtime.

Important members include:

- `ctx.manifest` — validated manifest object
- `ctx.log` — plugin-specific logger
- `ctx.state` — transient in-memory state dictionary
- `ctx.subscribe(topic)` — permission-checked event subscription decorator
- `ctx.emit(topic, payload)` — permission-checked event emission
- `ctx.create_task(coro, name=...)` — tracked async task creation
- `ctx.get_secret(key)` — read from the plugin Vault namespace
- `ctx.set_secret(key, value)` — write into the plugin Vault namespace

### Vault isolation

Secrets are separated by module identity:

- core components use `core/<manifest.id>`
- plugins use `plugins/<manifest.id>`
- both are stored inside the Vault mount `lyndrix` using KV v2

This means plugins do not automatically share secret space with each other.

## Event integration

Plugins should communicate through the event bus via `ctx` instead of importing the global bus directly.

Common topics worth knowing include:

- `vault:ready_for_data`
- `system:boot_complete`
- `db:connected`
- `plugin:install_started`
- `plugin:installed`
- `plugin:install_failed`
- `plugin:files_changed`
- `git:status_update`
- `ui:needs_refresh`

Important rules:

- every subscribed topic must be declared in `permissions.subscribe`
- every emitted topic must be declared in `permissions.emit`
- unauthorized event access is blocked and logged by Lyndrix

## Dependencies inside a plugin

If a plugin contains a `requirements.txt`, Lyndrix installs those dependencies during plugin installation or upgrade into the plugin-local `vendor/` folder.

This design keeps plugin dependencies isolated from the core application environment.

Notes:

- installation happens before the plugin is moved into its final runtime directory
- install failures abort the operation and trigger cleanup
- suspicious requirement lines such as unsafe local-path style entries are rejected

## Installation and versioning

Lyndrix can install plugins directly from GitHub repositories.

Supported flows:

- install the default branch as `latest`
- install a specific tag such as `v1.2.3`
- upgrade an existing plugin through an atomic staging and swap workflow

During installation, Lyndrix:

1. resolves repository metadata through the GitHub API
2. downloads a branch or tag archive
3. extracts the archive into a staging directory
4. validates extraction paths to prevent ZIP path traversal
5. installs dependencies into `vendor/` if needed
6. moves the plugin into `/app/plugins`
7. emits plugin lifecycle events

## Desired plugin state

`LYNDRIX_PLUGINS_DESIRED` allows operators to define plugins that should exist after reconciliation.

Format:

```text
https://github.com/org/plugin-a@v1.2.0,https://github.com/org/plugin-b
```

Rules:

- omit `@version` to mean `latest`
- set `LYNDRIX_PLUGINS_AUTO_UPDATE=true` to auto-update `latest` plugins during reconcile

## Authentication extensions

Plugins can register custom authentication providers at runtime through the event bus.

Relevant event:

- `auth:register_provider` with payload `{"provider": <AuthProvider instance>}`

If you do this, the provider ID must also be listed in `LYNDRIX_AUTH_PROVIDERS` so the provider chain can activate it.

## Minimal example

```python
from core.api import ModuleManifest
from nicegui import ui

manifest = ModuleManifest(
    id="lyndrix.plugin.hello",
    name="Hello Plugin",
    version="1.0.0",
    description="Example plugin",
    author="Example",
    type="PLUGIN",
    ui_route="/hello",
    permissions={"subscribe": ["system:boot_complete"], "emit": []},
)

async def setup(ctx):
    @ui.page('/hello')
    async def hello_page():
        ui.label('Hello from plugin')
```

## Best practices

- import from `core.api` whenever possible
- keep `manifest.id` stable across releases
- use `ctx.get_secret` and `ctx.set_secret` instead of plain files or env vars
- move long-running work into `ctx.create_task(...)`
- declare `min_core_version` when your plugin depends on newer core behavior
- document plugin behavior in the plugin repository itself

## Troubleshooting

### Plugin does not load

Check:

- `entrypoint.py` exists
- `manifest` is present and valid
- the folder name is import-safe
- the plugin import does not fail due to missing dependencies

### Plugin is blocked

Check:

- whether all dependencies in `dependencies` are installed and active
- whether the dependent plugins use the expected `manifest.id`

### Secret access returns nothing

Check:

- whether Vault is open
- whether the plugin waited for `vault:ready_for_data`
- whether the secret was written under the plugin's own namespace

### Upgrade fails

Check:

- whether the requested tag exists
- whether the downloaded archive is valid
- whether `requirements.txt` can be installed cleanly
