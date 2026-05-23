# System Architecture

This page describes the runtime architecture of Lyndrix Core and how the main subsystems interact during startup and normal operation.

## Architectural style

Lyndrix Core is an event-driven platform runtime.

Core services do not rely on a single monolithic startup function. Instead, they subscribe to well-defined bus topics, react when prerequisites become available, and emit new events when their own responsibilities are complete.

This design keeps components loosely coupled and makes plugin integration easier.

## Main building blocks

### Application shell

- `app/main.py`
- `app/ui/*`

Responsibilities:

- create the FastAPI and NiceGUI runtime
- wire middleware and UI routes
- emit startup events such as `system:started`
- host the maintenance and boot-related UI behavior

### Event bus

- `app/core/bus.py`

Responsibilities:

- topic-based event delivery
- support for sync and async handlers
- tracked background tasks with centralized error logging
- selective payload redaction or summarization for noisy and sensitive topics

### Configuration

- `app/config.py`

Responsibilities:

- load environment-backed settings
- provide derived values such as `DATABASE_URL`
- parse plugin desired-state configuration
- support environment, `.env`, and Vault-based configuration lookups

### Core components

- `app/core/components/*`

Responsibilities:

- Vault bootstrap and secret readiness
- database initialization and reconnect handling
- IAM bootstrap and provider-chain activation
- boot orchestration and module loading
- plugin lifecycle and marketplace sync
- UI features such as dashboard, settings, and notifications

## Simplified boot sequence

The startup flow is roughly:

1. the application emits `system:started`
2. the Vault component checks initialization and seal state
3. once Vault is ready, it emits `vault:opened`
4. the database component reacts to `vault:opened` and starts connecting
5. once the database is ready, it emits `db:connected`
6. the auth component initializes IAM and emits `iam:ready`
7. the boot component reacts to `iam:ready`, changes the boot phase, and triggers module loading
8. module discovery loads core modules and plugins
9. once startup succeeds, Lyndrix emits `system:boot_complete`

If a critical phase fails, Lyndrix can move into `system:maintenance_mode`.

## Boot phases

The Boot component maintains an explicit state machine:

- `waiting_core`
- `loading_modules`
- `ready`
- `failed`

The current phase is published through `system:boot_phase`, allowing the UI to reflect progress and failures.

## Plugin architecture

The plugin system is split across two main layers.

### ModuleManager

Key responsibilities:

- discover built-in core components and user plugins
- import `entrypoint.py` files
- validate manifests
- create `ModuleContext` objects
- restore plugin activation state from the database
- enforce dependency-aware activation
- handle reload, unload, enable, and disable flows

### PluginService

Key responsibilities:

- download plugin archives from GitHub
- resolve version tags
- validate archive extraction paths
- install plugin dependencies into `vendor/`
- perform staging-based install and upgrade operations
- manage marketplace metadata and collection sync

## Authentication architecture

The authentication subsystem includes:

- local username/password login
- optional LDAP authentication
- optional OIDC login
- a provider registry that preserves runtime order
- plugin-registered providers through `auth:register_provider`

The auth subsystem emits `iam:ready` when the schema, bootstrap users, and provider chain are ready.

## Data ownership

Lyndrix uses multiple persistence layers for different concerns:

- **MariaDB** for relational platform state such as users, groups, and plugin activation data
- **Vault** for secrets and secure runtime configuration
- **local storage paths** for logs, plugin data, temporary runtime data, and encrypted Vault bootstrap material

## Event-driven integration

The event bus is the contract between components.

Examples:

- Vault signals readiness to Database
- Database signals readiness to Auth
- Auth signals readiness to Boot
- PluginService signals file changes to ModuleManager
- Git sync status feeds plugin marketplace refresh behavior
- Notifications expose a normalized outbound event stream

For a full topic reference, see [core-components/events.md](core-components/events.md).

## UI-facing architecture

The UI is not a separate backend service. Instead, it is part of the same application process and reacts to shared runtime state.

Examples:

- boot state can be reflected in the UI via `system:boot_phase`
- dashboard widgets can be contributed by plugins
- settings and login views are owned by dedicated core components
- maintenance mode can be activated by backend components and surfaced in the frontend

## Source map

Helpful files for architecture work:

- `app/main.py`
- `app/config.py`
- `app/core/bus.py`
- `app/core/services.py`
- `app/core/components/boot/logic/boot_service.py`
- `app/core/components/vault/logic/vault_service.py`
- `app/core/components/database/logic/db_service.py`
- `app/core/components/auth/logic/auth_service.py`
- `app/core/components/plugins/logic/manager.py`
- `app/core/components/plugins/logic/plugin_service.py`
