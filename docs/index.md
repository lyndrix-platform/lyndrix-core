# Lyndrix Core Documentation

Welcome to the technical documentation for Lyndrix Core.

Lyndrix Core is the runtime foundation for the Lyndrix platform. It combines a FastAPI backend, a NiceGUI frontend, an event-driven component model, Vault-based secret management, and a database-backed plugin lifecycle.

## What this documentation covers

This documentation set is written for three main audiences:

- **Platform operators** who need to boot, configure, and run Lyndrix Core safely
- **Plugin developers** who want to extend the platform through the supported plugin API
- **Core contributors** who need an overview of the internal architecture and component responsibilities

## Quick navigation

- [Installation and deployment](deployment.md)
- [Plugin development guide](plugins.md)
- [Plugin ecosystem](ecosystem.md)
- [Security and Vault](security.md)
- [System architecture](architecture.md)
- [Core components reference](core-components/index.md)

## Platform overview

Lyndrix Core brings together the following building blocks:

- **FastAPI + NiceGUI** for API endpoints, authentication flows, and the web UI
- **A global event bus** for low-coupling communication between core services and plugins
- **Vault-first secret handling** with bootstrap, unseal, and mount management
- **SQLAlchemy + MariaDB** for persistent platform state and plugin activation status
- **A plugin runtime** with lifecycle hooks, per-plugin dependency isolation, and update workflows
- **A messaging gateway and notification router** for two-way provider messaging (Discord, …) and operator-controlled notification routing
- **A theming engine** with token/component themes and plugin-scoped overrides
- **API authentication** with a system key, per-user API keys, and a permission system

## Current capabilities

At the current repository state, Lyndrix Core includes:

- Boot orchestration with the phases `waiting_core`, `loading_modules`, `ready`, and `failed`
- Vault initialization and unseal support, including optional auto-flow via `LYNDRIX_MASTER_KEY`
- Persistent plugin state restoration from the database
- Plugin installation from GitHub repositories and version tags
- Per-plugin `requirements.txt` installation into a private `vendor/` directory
- Desired-state plugin reconciliation through `LYNDRIX_PLUGINS_DESIRED`
- Configurable authentication providers such as `local`, `ldap`, and `oidc`
- A stable plugin-facing API surface exposed through `core.api`

## Recommended reading path

If you are new to the project, this order works well:

1. Read [Installation and deployment](deployment.md) to understand the runtime setup
2. Read [Security and Vault](security.md) to understand the secret-management model
3. Read [System architecture](architecture.md) for the boot flow and subsystem overview
4. Use [Core components reference](core-components/index.md) for component-level details
5. Read [Plugin development guide](plugins.md) if you are building extensions

## Key source locations

Helpful starting points in the repository:

- `app/main.py` — application entry point, middleware, and route wiring
- `app/config.py` — central environment-backed configuration
- `app/core/bus.py` — the global event bus
- `app/core/components/*` — internal core components
- `app/core/api/__init__.py` — stable imports intended for plugin authors

## Documentation build

The documentation site is generated from `mkdocs.yml` and the Markdown files in `docs/`. The repository workflow builds it with `zensical build` and writes the generated site into `.cache/site`.
