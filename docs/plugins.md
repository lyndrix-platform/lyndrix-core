# Plugin Development Guide

This guide explains how to build plugins for Lyndrix Core and how the core runtime interacts with them.

---

## Plugin model

A Lyndrix plugin is a Python package inside `/app/plugins/<plugin_folder>` with an `entrypoint.py` module.

At minimum, a plugin provides:

- a `manifest`
- a `setup(ctx)` function

The manifest tells Lyndrix what the plugin is, what permissions it needs, and how it should behave during activation.

---

## Directory structure

### Minimal plugin

A simple, single-page plugin:

```text
app/plugins/my_plugin/
├── entrypoint.py           # manifest + lifecycle hooks
├── requirements.txt        # optional
├── vendor/                 # generated during install — do NOT commit this
├── assets/                 # optional static assets
└── locales/                # optional translation files, auto-registered
```

### Recommended structure for non-trivial plugins

For any plugin with persistent data, multiple UI pages, or business logic beyond a single page, it is **strongly recommended** to move all logic into an `./app/` sub-package and keep `entrypoint.py` as a thin wiring layer.

This is the pattern used by the reference implementation `lyndrix-plugin-server-manager`.

```text
app/plugins/my_plugin/
├── entrypoint.py           # manifest + lifecycle hooks only — no business logic
├── requirements.txt        # optional
├── vendor/                 # generated during install — do NOT commit this
├── assets/                 # optional static assets
├── locales/                # optional translation files
├── examples/               # optional: example YAML/JSON configs for operators
└── app/
    ├── __init__.py
    ├── model/              # data layer: ORM models, DB session helpers, external loaders
    ├── controller/         # business logic: service singletons, event handlers
    └── ui/                 # NiceGUI components: pages, widgets, dialogs
```

**Layer responsibilities:**

| Layer | Responsibility | Examples |
|---|---|---|
| `model/` | ORM models, DB session, external YAML/JSON loaders | `models.py`, `database.py`, `catalog.py` |
| `controller/` | Business logic, CRUD, service singletons, event emission | `service.py`, `configurator/` |
| `ui/` | NiceGUI pages, widgets, dialogs | `overview.py`, `widget.py` |

**Rules for the `./app/` pattern:**

- UI components must never write to the database directly — all DB access goes through the controller layer
- `entrypoint.py` must only contain: `manifest`, lifecycle hook functions (`setup`, `teardown`, `render_settings_ui`, `render_dashboard_widget`), and imports — no business logic, no inline UI components
- If your plugin holds state (DB sessions, caches, runtime configuration), expose a single service object from `app/controller/service.py` and import it where needed — avoid creating multiple instances
- Include an `examples/` directory if your plugin requires operator-provided configuration files (YAML, JSON) — this helps operators understand the expected format without reading source code

**Naming rules:**

- the folder name must be a valid Python identifier
- if a repository name contains `-`, Lyndrix normalizes it to `_` for imports
- `vendor/` is generated at install time — add it to your `.gitignore`

---

## Stable plugin API

Plugin code should import from `core.api` rather than from internal core modules.

```python
from core.api import __api_version__, ModuleManifest
```

Current stable API version:

- `__api_version__ = "1.0.0"`

The goal is to give plugin authors a stable surface even when the internal core structure evolves.

### Database integration

If your plugin needs persistent storage, use the `Base` and `db_instance` exported from `core.api`:

```python
from core.api import db_instance, Base
```

- Inherit your SQLAlchemy models from `Base` — this registers them with the shared metadata
- Use `db_instance.is_connected` to guard DB operations during setup
- Call your table bootstrap logic when the `db:connected` event fires (see *Event integration*)
- Never write raw SQL against the core database schema — only interact with tables your plugin owns

Example model:

```python
from core.api import Base
from sqlalchemy import Column, Integer, String

class MyRecord(Base):
    __tablename__ = "my_plugin_records"
    id = Column(Integer, primary_key=True)
    name = Column(String(255))
```

---

## Manifest fields

`ModuleManifest` supports the following fields:

| Field | Required | Description |
|---|---|---|
| `id` | ✅ | Unique plugin ID — typically `lyndrix.plugin.<name>` — must be stable across releases |
| `name` | ✅ | Display name shown in the marketplace and plugin manager |
| `version` | ✅ | Plugin version string — should match the git tag for installable releases |
| `description` | recommended | Short description shown in marketplace and plugin cards |
| `author` | recommended | Author name or organization |
| `icon` | recommended | Material icon name for the plugin card |
| `type` | ✅ | Use `PLUGIN` for user plugins |
| `ui_route` | if using UI | Route mounted by the plugin |
| `permissions.subscribe` | as needed | Topics the plugin may subscribe to — unauthorized access raises a `PermissionError` |
| `permissions.emit` | as needed | Topics the plugin may emit — unauthorized access raises a `PermissionError` |
| `permissions.vault_paths` | as needed | Additional Vault paths beyond the plugin's default namespace |
| `settings_schema` | optional | Reserved — not yet in stable use; omit or leave as `{}` |
| `dependencies` | as needed | List of `{id, version_constraint}` entries for required plugins/modules |
| `min_core_version` | recommended | Minimum Lyndrix API version required by this plugin |
| `auto_enable_on_install` | recommended | Whether the plugin auto-activates on install — **default is `True`; set to `False` for plugins that require configuration before first use** |
| `repo_url` | recommended | Source repository URL — used for update checks and marketplace metadata — must point to the canonical `lyndrix-platform` org URL |

**Important notes on `repo_url`:**

The `repo_url` field must always point to the current canonical repository under the `lyndrix-platform` organization. A stale or wrong URL will break the update and marketplace flow.

```python
# ✅ Correct
repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-my-name",

# ❌ Wrong — personal fork, stale URL
repo_url="https://github.com/my-personal-account/lyndrix-my-name",
```

---

## Lifecycle hooks

### Required hook

- `setup(ctx)` — can be synchronous or asynchronous. Called when the plugin becomes active.

### Optional hooks

- `teardown(ctx)` — cleanup during deactivation or unload
- `render_settings_ui(ctx)` — render plugin settings inside the platform settings UI
- `render_dashboard_widget(ctx)` — render a compact widget on the main dashboard

### Lifecycle rules

- `setup(ctx)` must not block indefinitely — long-running work belongs in `ctx.create_task(...)`
- If your plugin needs the database, guard DB operations behind `db:connected` (see *Event integration*)
- If setup raises an unhandled exception, the plugin status transitions to `failed` and a `plugin:setup_failed` event is emitted

---

## Runtime states

Plugins can move through the following internal states:

| State | Meaning |
|---|---|
| `initializing` | Plugin is being loaded and `setup()` is running |
| `active` | Plugin is fully operational |
| `disabled` | Manually disabled by an operator |
| `blocked` | Enabled, but a declared dependency is missing or inactive |
| `failed` | `setup()` raised an unhandled exception |
| `degraded` | Loaded but manifest validation found issues |

---

## ModuleContext

Each plugin receives a `ModuleContext` instance named `ctx`. This is the supported bridge into the core runtime.

| Member | Description |
|---|---|
| `ctx.manifest` | Validated manifest object |
| `ctx.log` | Plugin-specific logger |
| `ctx.state` | Transient in-memory state dictionary — **lost on restart** |
| `ctx.subscribe(topic)` | Permission-checked event subscription decorator |
| `ctx.emit(topic, payload)` | Permission-checked event emission |
| `ctx.create_task(coro, name=...)` | Tracked async task creation |
| `ctx.get_secret(key)` | Read from the plugin Vault namespace |
| `ctx.set_secret(key, value)` | Write into the plugin Vault namespace |

### Choosing the right storage mechanism

| What you need to store | Use |
|---|---|
| Temporary runtime flags, cached objects | `ctx.state` |
| Credentials, tokens, sensitive configuration | `ctx.get_secret` / `ctx.set_secret` |
| Persistent business data that must survive restarts | DB models (via `Base`) |

> ⚠️ `ctx.state` is **in-memory only**. Do not store anything in `ctx.state` that must survive a process restart or plugin reload.

### Vault isolation

Secrets are separated by module identity:

- core components use `core/<manifest.id>`
- plugins use `plugins/<manifest.id>`
- both are stored inside the Vault mount `lyndrix` using KV v2

Plugins do not automatically share secret space with each other.

---

## Event integration

Plugins should communicate through the event bus via `ctx` instead of importing the global bus directly.

```python
# ✅ Correct — uses ctx, respects permission declarations
@ctx.subscribe("db:connected")
async def _on_db_ready(payload):
    ...

# ❌ Wrong — bypasses permission checking
from core.bus import global_bus
global_bus.subscribe("db:connected")(my_handler)
```

**Permission enforcement:**

- Every subscribed topic must be declared in `permissions.subscribe`
- Every emitted topic must be declared in `permissions.emit`
- Unauthorized access raises a `PermissionError` immediately — it does not silently fail

**Common platform topics:**

| Topic | When it fires |
|---|---|
| `vault:ready_for_data` | Vault is unsealed and ready for secret access |
| `system:boot_complete` | All core components have finished booting |
| `db:connected` | Database connection established (also fires on reconnect) |
| `plugin:install_started` | A plugin installation has begun |
| `plugin:installed` | A plugin installation completed successfully |
| `plugin:install_failed` | A plugin installation failed |
| `plugin:files_changed` | Plugin files on disk were modified |
| `plugin:setup_failed` | A plugin's `setup()` raised an unhandled exception |
| `git:status_update` | Git component has a new status |
| `ui:needs_refresh` | The UI should refresh its state |

---

## Dependencies inside a plugin

If a plugin contains a `requirements.txt`, Lyndrix installs those dependencies during plugin installation or upgrade into the plugin-local `vendor/` folder.

This keeps plugin dependencies isolated from the core application environment.

**Notes:**

- installation happens before the plugin is moved into its final runtime directory
- install failures abort the operation and trigger cleanup
- suspicious requirement lines (e.g. local-path style entries) are rejected
- `vendor/` must be in your `.gitignore` — it is generated by Lyndrix and should never be committed to source control
- vendored packages do not have priority over core packages — they cannot shadow or override core dependencies

**Declaring inter-plugin dependencies:**

Use the `dependencies` manifest field to declare that your plugin requires another plugin to be active:

```python
dependencies=[
    {"id": "lyndrix.plugin.other_plugin", "version_constraint": ">=1.2.0"}
]
```

- Lyndrix checks that the dependency is installed and active before activating your plugin
- `version_constraint` is evaluated against the dependency's `manifest.version`
- A plugin with an unmet or version-incompatible dependency enters `blocked` state

---

## Installation and versioning

Lyndrix can install plugins directly from GitHub repositories.

**Supported flows:**

- install the default branch as `latest`
- install a specific tag such as `v1.2.3`
- upgrade an existing plugin through an atomic staging and swap workflow

**During installation, Lyndrix:**

1. resolves repository metadata through the GitHub API
2. downloads a branch or tag archive
3. extracts the archive into a staging directory
4. validates extraction paths to prevent ZIP path traversal
5. verifies that the extracted `manifest.id` matches the expected plugin identity
6. installs dependencies into `vendor/` if needed
7. moves the plugin into `/app/plugins`
8. emits plugin lifecycle events

**Versioning guidance for plugin authors:**

- use semantic versioning: `MAJOR.MINOR.PATCH`
- `manifest.version` must match the git tag used for the release
- tag releases with `git tag v0.1.0 && git push --tags`
- `latest` always tracks the default branch HEAD — only pin to a tag for production deployments
- maintain a `CHANGELOG.md` in your plugin repository so operators know what they are upgrading to

---

## Desired plugin state

`LYNDRIX_PLUGINS_DESIRED` allows operators to define plugins that should exist after reconciliation.

Format:

```text
https://github.com/lyndrix-platform/plugin-a@v1.2.0,https://github.com/lyndrix-platform/plugin-b
```

Rules:

- omit `@version` to mean `latest`
- set `LYNDRIX_PLUGINS_AUTO_UPDATE=true` to auto-update `latest` plugins during reconcile

---

## Authentication extensions

Plugins can register custom authentication providers at runtime through the event bus.

Relevant event:

- `auth:register_provider` with payload `{"provider": <AuthProvider instance>}`

If you do this, the provider ID must also be listed in `LYNDRIX_AUTH_PROVIDERS` so the provider chain can activate it.

---

## Testing your plugin

Plugin code that follows the `./app/` pattern is independently testable — the `model/` and `controller/` layers have no dependency on a running core.

**Recommended test setup:**

- create a `tests/` directory alongside `app/`
- mirror the core toolchain: include `pytest`, `pytest-asyncio`, `pytest-cov`, `mypy`, `ruff`, and `black` in a `requirements-dev.txt`
- unit-test `model/` and `controller/` logic without a running Lyndrix instance
- mock `ctx` for lifecycle hook tests — the `ModuleContext` interface is stable

```text
app/plugins/my_plugin/
├── entrypoint.py
├── app/
│   ├── model/
│   ├── controller/
│   └── ui/
├── tests/
│   ├── test_service.py
│   └── test_models.py
└── requirements-dev.txt
```

---

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
    icon="waving_hand",
    type="PLUGIN",
    ui_route="/hello",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-hello",
    permissions={"subscribe": ["system:boot_complete"], "emit": []},
)

async def setup(ctx):
    @ui.page('/hello')
    async def hello_page():
        ui.label('Hello from plugin')
```

---

## Best practices

**Manifest and identity:**

- keep `manifest.id` stable across releases — changing it creates a new plugin identity
- set `auto_enable_on_install=False` for plugins that require configuration before first use
- always set `repo_url` to the canonical `lyndrix-platform` org URL
- declare `min_core_version` when your plugin depends on newer core behavior

**Code structure:**

- import from `core.api` whenever possible — never from internal `core.*` modules
- use the `./app/` sub-package pattern for any plugin with more than trivial logic
- keep `entrypoint.py` as a pure wiring layer — manifest, hooks, and imports only
- do not write to the filesystem from plugin code; use `ctx.state`, Vault secrets, or plugin-owned DB tables

**State and secrets:**

- use `ctx.get_secret` / `ctx.set_secret` for credentials and sensitive configuration — never plain files or environment variables
- use plugin-owned `Base` models for data that must survive restarts
- reserve `ctx.state` for transient runtime objects only

**Async and tasks:**

- move long-running or background work into `ctx.create_task(...)`
- never block in `setup(ctx)` — use `db:connected` and `vault:ready_for_data` events to defer initialization

**Events:**

- declare all subscribed and emitted topics in `permissions` — unauthorized access raises immediately
- emit well-documented events from your service layer so other plugins can integrate with your plugin

**Releases:**

- tag every release with a semver git tag that matches `manifest.version`
- maintain a `CHANGELOG.md`
- add `vendor/` to your `.gitignore`

---

## Troubleshooting

### Plugin does not load

Check:

- `entrypoint.py` exists and imports without errors
- `manifest` is present and is a valid `ModuleManifest` instance
- the folder name is a valid Python identifier
- all entries in `requirements.txt` can be installed cleanly

### Plugin is blocked

Check:

- all entries in `dependencies` are installed and in `active` state
- the dependency plugin IDs match exactly (check `manifest.id` in each dependency's entrypoint)
- `version_constraint` entries are satisfiable against the installed dependency versions

### Plugin is in `failed` state

Check:

- the `plugin:setup_failed` event payload for the exception message
- `setup(ctx)` does not raise during DB bootstrap or Vault access
- the plugin waited for the appropriate event (`db:connected`, `vault:ready_for_data`) before accessing those subsystems

### Secret access returns nothing

Check:

- whether Vault is unsealed (wait for `vault:ready_for_data`)
- whether the secret was written under the plugin's own namespace (`plugins/<manifest.id>`)

### Upgrade fails

Check:

- whether the requested tag exists in the source repository
- whether the downloaded archive is valid and the `manifest.id` matches the expected plugin identity
- whether `requirements.txt` installs cleanly in isolation

### Permission error on emit or subscribe

Check:

- whether the topic is declared in `permissions.emit` (for `ctx.emit`) or `permissions.subscribe` (for `ctx.subscribe`)
- unauthorized access raises a `PermissionError` — it will appear in the plugin log and in the platform error stream
