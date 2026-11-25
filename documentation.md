# Lyndrix Core — Documentation

> Version 0.4.0 · "First Light Alpha"

Lyndrix Core is the Python runtime framework behind the Lyndrix platform. It combines **FastAPI** (REST backend) and **NiceGUI** (optional in-process frontend) into an extensible, event-driven monolith that can be supplemented with arbitrary functionality through plugins. Every plugin receives the same platform services — authentication, database, secrets vault, event bus, theming and internationalization — without having to build its own infrastructure.

---

## Table of contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Configuration](#configuration)
4. [UI engine](#ui-engine)
5. [Authentication](#authentication)
6. [Vault integration](#vault-integration)
7. [Database](#database)
8. [Plugin system](#plugin-system)
9. [Messaging gateway](#messaging-gateway)
10. [Internationalization](#internationalization)
11. [Theming](#theming)
12. [REST API overview](#rest-api-overview)
13. [Development](#development)

---

## Overview

Lyndrix Core starts as a single Docker service and bootstraps itself completely: HashiCorp Vault is initialized and unsealed, the database is connected, the IAM chain is built, and finally all configured plugins are loaded. Developers write only plugin code — the entire infrastructure is provided by Core.

**Core principles:**

- **Plugin-first**: Every function outside the core is developed as a plugin. Core provides a stable API surface (`from core.api import …`) that never changes.
- **API-first**: Every plugin function is reachable through the REST backend. Frontends (React or NiceGUI) are pure consumers of the API.
- **Event-driven**: Components communicate exclusively through the global event bus (`bus.py`). No direct dependencies between plugins.
- **Zero-trust secrets**: All secrets are stored in Vault. Environment variables can always be overridden, but they always win over Vault values.

---

## Architecture

### Boot sequence

The startup runs in a fixed, event-driven order:

```
system:started
  └─► Vault: check init/seal ──► vault:opened
        └─► connect database ──► db:connected
              └─► build IAM chain ──► iam:ready
                    └─► load core modules + plugins ──► system:boot_complete
```

The boot state machine knows the following states:

| State | Meaning |
|---|---|
| `waiting_core` | Waiting for Vault and DB |
| `loading_modules` | Plugins are being initialized |
| `ready` | System fully started |
| `failed` | Fatal error during startup |

### Important source files

| File | Task |
|---|---|
| `app/main.py` | FastAPI + NiceGUI setup, middleware, route registration, startup hook |
| `app/config.py` | `Settings` (pydantic-settings); hierarchy: OS env > `.env` > Vault > default |
| `app/core/bus.py` | `GlobalEventBus` — `subscribe(topic)` decorator + `emit(topic, payload)` |
| `app/core/services.py` | Single import point for all singleton services |
| `app/core/api/__init__.py` | Stable plugin API surface (`__api_version__ = "1.1.0"`) |
| `app/core/i18n.py` | Translation engine, `t()`, catalog versioning, `client_namespaces()` |

### Component anatomy

Every core component and every plugin follows the same canonical structure:

```
<component>/
├── model/        # SQLAlchemy models, Pydantic schemas, YAML loaders
├── logic/        # Service singletons, CRUD, event handlers
├── api/          # FastAPI routers (API-first — both UIs use the API exclusively)
└── ui/
    ├── react/    # React frontend (Vite IIFE bundle → app/ui/static/ui_bundle.js)
    └── nicegui/  # NiceGUI pages and widgets (opt-in)
```

### Event bus

```python
from core.api import event_bus

# Subscribe (in plugin setup):
@ctx.subscribe('db:connected')
async def on_db_ready(payload):
    await my_service.initialize()

# Send:
await ctx.emit('myplugin:something_happened', {"key": "value"})
```

Plugins declare subscribed and emitted topics in the manifest under `permissions`. The bus does not guarantee any ordering between independent subscribers.

### Platform events

| Topic | When |
|---|---|
| `vault:ready_for_data` | Vault unsealed, secrets safely readable/writable |
| `db:connected` | Database ready (also fired again on reconnect) |
| `system:boot_complete` | All core components and plugins fully started |
| `ui:needs_refresh` | Frontend should reload its state |
| `messaging:outbound` | Plugin wants to send an outbound message |

---

## Configuration

All settings are managed through the `Settings` class in `app/config.py`. The resolution order is:

```
OS environment variable  >  .env file  >  Vault KV  >  default value
```

OS environment variables **always win** — they are not overwritten after the Vault hydration.

### Core variables

| Variable | Default | Meaning |
|---|---|---|
| `VAULT_URL` | `http://vault:8200` | Address of the HashiCorp Vault |
| `LYNDRIX_MASTER_KEY` | — | Optional auto-unseal key |
| `DB_HOST` | `db` | MariaDB hostname |
| `DB_PORT` | `3306` | MariaDB port |
| `DB_NAME` | `lyndrix` | Database name |
| `DB_USER` | `lyndrix` | Database user |
| `DB_PASSWORD` | — | Database password |
| `LYNDRIX_AUTH_PROVIDERS` | `local` | Ordered, comma-separated auth chain: `local`, `ldap`, `oidc` |
| `LYNDRIX_PLUGINS_DESIRED` | — | Comma-separated plugin specs that are reconciled at startup |
| `LYNDRIX_PLUGINS_AUTO_UPDATE` | `false` | Automatically update `latest` plugins on restart |
| `LYNDRIX_SYSTEM_API_KEY` | — | Master M2M API key (disabled if not set) |
| `LYNDRIX_UI_ENGINE` | `api` | Active UI engines, comma-separated: `api`, `react`, `nicegui` |
| `DEFAULT_LOCALE` | `de` | Default language of the platform |
| `DEFAULT_THEME_ID` | `default` | ID of the active theme |
| `SECRET_KEY` | — | JWT signing key |
| `CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |

### Vault hydration

When the `vault:opened` event is received, Core calls `settings.hydrate_from_vault()`. KV values are read from the `lyndrix` mount and applied to `Settings` — **provided the corresponding environment variable is not already set**.

---

## UI engine

As of v0.4.0, `LYNDRIX_UI_ENGINE` is a **comma-separated multi-value list**. NiceGUI is no longer active by default.

| Value | Meaning |
|---|---|
| `api` | REST API only, no UI started (default, suitable for M2M deployments) |
| `react` | `lyndrix-ui` React SPA is served as a static bundle |
| `nicegui` | In-process NiceGUI frontend is enabled (opt-in) |

Examples:

```bash
# REST API only (default):
LYNDRIX_UI_ENGINE=api

# Enable React UI:
LYNDRIX_UI_ENGINE=api,react

# React UI + NiceGUI in parallel (development / migration):
LYNDRIX_UI_ENGINE=api,react,nicegui
```

The React UI (`lyndrix-ui`) communicates exclusively via relative `/api/…` paths. The bundle is served by Core under `/` and requires no separate server. Plugins deliver their own React bundle as `app/ui/static/ui_bundle.js` — Core mounts it under `/api/plugins/<id>/static/`.

---

## Authentication

### Provider chain

`LYNDRIX_AUTH_PROVIDERS` defines the order in which authentication attempts are evaluated. Each entry corresponds to one provider:

| Provider | Requirements |
|---|---|
| `local` | User data in the local MariaDB (default) |
| `ldap` | LDAP/Active Directory server (connection data in Vault) |
| `oidc` | OpenID Connect (client ID/secret in Vault) |

The first provider that knows the user "wins". Unknown users are passed on to the next provider.

### API keys

Every user can create multiple API keys with fine-grained permissions:

```
POST /api/auth/keys          → create key
GET  /api/auth/keys          → list own keys
DELETE /api/auth/keys/{id}   → delete key
```

Plugins protect their endpoints with:

```python
from core.api import require_permission

@router.get("/data")
async def get_data(identity = Depends(require_permission("api:read"))):
    ...
```

### System API key

`LYNDRIX_SYSTEM_API_KEY` is an immutable master key for M2M access (CI/CD, IaC pipeline). It is not assigned to any user and should **not** be used for interactive access.

---

## Vault integration

Lyndrix uses HashiCorp Vault as its only secrets store. Core bootstraps Vault completely:

1. **Init**: Vault is initialized with a single unseal key, which is stored encrypted.
2. **Unseal**: At startup, Vault is unsealed automatically (via `LYNDRIX_MASTER_KEY` or manually).
3. **KV v2 mount**: All secrets reside in the `lyndrix` KV mount.
4. **Health checks**: Core continuously monitors the Vault status and fires `vault:opened` / `vault:sealed` on state changes.

Plugins **never** access Vault directly. They use:

```python
# Via context (recommended):
await ctx.get_secret("my_secret_key")
await ctx.set_secret("my_secret_key", "value")

# Or via the Vault service:
from core.api import vault_service
value = await vault_service.kv_get("my_key")
```

---

## Database

Core uses **MariaDB** (MySQL-compatible) via **SQLAlchemy** (async). The shared `Base` class is used by Core and all plugins:

```python
from core.api import db_instance, Base

class MyModel(Base):
    __tablename__ = "my_plugin_table"
    id = Column(Integer, primary_key=True)
    ...
```

Plugins register their models on the `db:connected` event:

```python
@ctx.subscribe('db:connected')
async def on_db(payload):
    Base.metadata.create_all(db_instance.engine)
```

**Important:** Models that can be loaded again after the first start (e.g. during a plugin reload) must set `extend_existing=True` in `__table_args__` to avoid `InvalidRequestError`.

---

## Plugin system

### Manifest basics

```python
from core.api import ModuleManifest

manifest = ModuleManifest(
    id="lyndrix.plugin.<name>",    # Permanent — changing it creates a new plugin identity
    name="My Plugin",
    version="1.0.0",               # Must match the Git tag
    type="PLUGIN",
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-<name>",
    ui_route="/meinplugin",
    react_ui=True,                 # React bundle present
    i18n_namespace="meinplugin",   # i18next namespace for the React frontend
    auto_enable_on_install=False,  # False if configuration is required before first activation
    permissions={
        "subscribe": ["db:connected", "vault:ready_for_data"],
        "emit": ["meinplugin:etwas_passiert"],
    },
)
```

### Lifecycle

```python
def setup(ctx):
    """Synchronous entry point. Must NOT block."""
    ctx.register_routes(build_plugin_router(my_service))
    
    @ctx.subscribe('db:connected')
    async def on_db(payload):
        await my_service.init_db()
    
    # Start background tasks via create_task:
    ctx.create_task(my_service.run_background_loop(), name="meinplugin:loop")

def teardown(ctx):
    """Clean up when the plugin is disabled."""
    ...
```

### ModuleManager

`app/core/components/plugins/logic/manager.py` manages the complete plugin lifecycle:

- **Discovery**: Plugins are loaded as Python packages from the plugin directory or via `LYNDRIX_PLUGINS_DESIRED`.
- **Installation**: `plugin_service.py` loads plugins from GitHub (via Git tag), installs dependencies into `vendor/`, and activates the plugin.
- **Upgrade**: Same process — the new version is downloaded and the old one replaced.
- **Registry**: All active plugins can be queried via `ModuleManager.get_all()`.

### Plugin API surface

```python
from core.api import (
    ModuleManifest, ModuleContext, PluginHealthStatus,
    db_instance, Base,         # Database access
    APIRouter,                 # FastAPI router (re-exported)
    event_bus,                 # Global event bus
    notification_service,      # Send platform notifications
    auth_service,              # IAM access
    plugin_service,            # Install/upgrade plugins programmatically
    UIStyles,                  # Theming classes
    require_permission,        # Endpoint protection
)
```

**Rule:** Plugins import exclusively from `core.api`. Direct imports from `core.components.*` are forbidden and are rejected in code reviews.

---

## Messaging gateway

The messaging gateway enables plugins to send messages via external providers (Discord, Slack, etc.) and to route **replies** back to the triggering plugin.

### Canonical anatomy (as of v0.4.0)

```
app/core/components/messaging/
├── model/
│   ├── schemas.py          # Pydantic schemas: MessageSeverity, ActionButton,
│   │                       # OutboundMessage, InboundMessage, DeliveryResult
│   └── pending_action.py   # SQLAlchemy PendingActionRecord (correlation tracking)
└── logic/
    ├── adapter.py          # GatewayAdapter ABC, GatewayCapability, ProviderConfigField
    ├── correlation.py      # Correlation of incoming replies
    ├── gateway.py          # MessagingGateway singleton
    └── internal_adapter.py # In-process adapter (platform-internal notifications)
```

### Usage from plugins

```python
# Send outbound message:
await ctx.emit('messaging:outbound', {
    "provider": "discord",
    "message": "Deployment started!",
    "severity": "info",
    "action_buttons": [
        {"label": "Cancel", "action_id": "cancel_deploy", "style": "danger"}
    ],
    "correlation_id": "deploy-42",
})

# Receive incoming reply:
@ctx.subscribe('messaging:inbound')
async def on_reply(payload):
    if payload.get("action_id") == "cancel_deploy":
        await my_service.cancel()
```

### Provider adapters

Custom providers implement `GatewayAdapter` from `core.components.messaging.logic.adapter` and register themselves with the `MessagingGateway`. The notification router (`notification_router`) decides, based on user settings, which provider receives which notification.

---

## Internationalization

Lyndrix strictly separates two translation dialects by namespace:

### Dialects

| Namespace group | Placeholder | Consumer |
|---|---|---|
| Core/NiceGUI (`core`, `auth`, `plugins`, …) | `%{name}` | `core.i18n.t()` internally |
| React/i18next (`ui`, `settings`, plugin NS) | `{{name}}` + `_one`/`_other` | i18next client in the browser |

The allowlist of namespaces delivered to clients is `I18NEXT_NAMESPACES = {"ui", "settings"}` in `app/core/i18n.py`. Plugins extend this allowlist through the `i18n_namespace` field in the manifest.

### HTTP catalog API

Two unauthenticated endpoints, reachable already during boot (the login page must be localized):

```
GET /api/i18n/locales
→ { default, supported[], client_namespaces[], version }

GET /api/i18n/{locale}?ns=ui,settings&v=<version>
→ 200: { locale, version, resources: { ns: { key: value } } }
→ 304: if v == current version (caching)
→ 404: unknown locale
```

The catalog version is a content fingerprint over all registered locale files. It updates automatically when a plugin registers its `locales/` directory.

### Checking i18n keys

```bash
python scripts/check_i18n_keys.py
```

English (`en`) is the source locale. Non-English locales are deep-merged over the English tree at delivery time, so missing translations automatically fall back to English.

---

## Theming

The theming system consists of two layers:

### Layer 1 — token layer

`assets/themes/<id>/tokens.json` defines semantic color tokens with `light` and `dark` variants:

```json
{
  "light": {
    "primary": "#00d4ff",
    "bg_body": "#f8fafc",
    "text_muted": "#64748b"
  },
  "dark": {
    "primary": "#00d4ff",
    "bg_body": "#0a0f1e",
    "text_muted": "#94a3b8"
  }
}
```

### Layer 2 — component layer

`assets/themes/<id>/components.json` maps every `UIStyles` class name to a Tailwind string:

```json
{
  "CARD_BASE": "rounded-xl border border-zinc-700 bg-zinc-900 p-4",
  "TITLE_H2": "text-xl font-bold text-white",
  "TEXT_MUTED": "text-zinc-400 text-sm"
}
```

At startup, `_hydrate_ui_styles()` overwrites the `UIStyles` class attributes with the values from `components.json`, so all downstream components automatically use the active theme.

### Usage in plugins

```python
from core.api import UIStyles

ui.card().classes(UIStyles.CARD_BASE)
ui.label("Title").classes(UIStyles.TITLE_H2)
ui.label("Hint").classes(UIStyles.TEXT_MUTED)

# Plugin-specific overrides (only for this plugin, not global):
def setup(ctx):
    ctx.register_theme_overrides({
        "CARD_BASE": "p-6 rounded-2xl border border-violet-700 bg-violet-950",
    })
```

### CSS variables (for inline styles / React)

| Variable | Usage |
|---|---|
| `--lx-bg` | Page background |
| `--lx-surface` | Card/panel background |
| `--lx-elevated` | Modal/dropdown background |
| `--lx-accent` | Primary accent color (cyan) |
| `--lx-accent-2` | Secondary accent color (sky blue) |
| `--lx-accent-3` | Tertiary accent color (violet) |
| `--lx-text` | Primary text |
| `--lx-text-muted` | Secondary text |
| `--lx-border` | Accent border |
| `--lx-border-soft` | Subtle border |
| `--lx-radius-sm/md/lg` | Border-radius tokens |
| `--lx-glow` | Box-shadow glow |
| `--lx-state-up/down/paused/unknown` | Monitor state colors |

### Creating a custom theme

1. `assets/themes/<id>/tokens.json` — copy of `default/tokens.json`, adjust colors.
2. `assets/themes/<id>/components.json` — copy of `default/components.json`, adjust Tailwind classes.
3. Missing keys are replaced by `UIStyles` default values — partial themes are valid.
4. Select the theme under **Settings → Appearance** or set `DEFAULT_THEME_ID=<id>`.

Alternatively: submit a ZIP with both JSON files via **Settings → Appearance → Upload theme**.

---

## REST API overview

All API endpoints begin with `/api/`. Authentication via `Authorization: Bearer <token>` or `X-API-Key: <key>`.

| Prefix | Authentication | Description |
|---|---|---|
| `GET /api/i18n/locales` | No | Available locales and catalog metadata |
| `GET /api/i18n/{locale}` | No | Translation catalog (i18next-compatible) |
| `POST /api/auth/login` | No | Login, returns access and refresh tokens |
| `POST /api/auth/refresh` | No | Renew token |
| `POST /api/auth/logout` | Yes | Invalidate session |
| `GET/POST/DELETE /api/auth/keys` | Yes | Manage API keys |
| `GET /api/vault/status` | No | Vault status (init/sealed/unsealed) |
| `POST /api/vault/init` | No | Initialize Vault |
| `POST /api/vault/unseal` | No | Unseal Vault |
| `GET /api/users` | Yes | User list |
| `GET/PUT/DELETE /api/users/{id}` | Yes | Read/edit/delete user |
| `GET /api/groups` | Yes | Groups |
| `GET /api/plugins` | Yes | List installed plugins |
| `POST /api/plugins/install` | Yes | Install plugin |
| `POST /api/plugins/{id}/enable` | Yes | Enable plugin |
| `GET/PUT /api/plugins/{id}/settings` | Yes | Read/write plugin settings |
| `GET /api/themes` | Yes | List themes |
| `GET /api/themes/{id}/css-vars` | Yes | CSS variables of the theme |
| `GET /api/system/config` | Yes | Runtime config snapshot |
| `GET /api/system/health` | No | Health check |
| `GET /api/events` | Yes | SSE event stream |
| `GET/PUT /api/me/profile` | Yes | Read/edit own profile |
| `POST/GET/DELETE /api/me/background` | Yes | Personal background image |
| `GET /api/users/{username}/background/{mode}` | No | Retrieve background image (public) |

Plugin endpoints are mounted under `/api/plugins/<plugin-id>/` and protected by `require_api_auth`. Plugins can attach their own public routers directly to the FastAPI app (e.g. for webhooks and SSE streams).

---

## Development

### Local dev stack

```bash
# Start stack (App + DB + Vault + docs preview)
docker compose -f docker/docker-compose.dev.yml up -d --build

# Endpoints:
#   App:          http://localhost:8081
#   Vault UI:     http://localhost:8200
#   Docs preview: http://localhost:8000
```

The dev compose file mounts `../app` directly into the container filesystem. Code changes are picked up immediately by uvicorn `--reload`. Plugin repos are mounted as sibling volumes (see `docker/docker-compose.dev.yml`).

### Quality assurance

```bash
# Linter
ruff check app/

# Formatter
black app/

# Type checking
mypy app/

# Tests
pytest

# Check i18n key parity (en is the source locale)
python scripts/check_i18n_keys.py
```

### Versioning scheme

Lyndrix Core follows [Semantic Versioning](https://semver.org). The current version is in `app/version.py`:

```python
__version__ = "0.4.0"
__release_date__ = "2026-06-28"
__codename__ = "First Light Alpha"
```

Releases are Git-tag driven. The helper script `../release_tag.py` tags and pushes multiple repos at once:

```bash
# From the lyndrix-dev workspace:
./release_tag.py --version 0.4.0
```

### Testing plugins locally

```bash
# In the plugin repo directory:
pip install -r requirements-dev.txt
pytest
mypy .
ruff check .
black --check .
```

Model and logic layers are testable without a running core. `ModuleContext` can be mocked for lifecycle tests (the interface is stable).
