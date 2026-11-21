# Lyndrix Core

Lyndrix Core is the central runtime core for modular internal-platform workloads, built on **FastAPI**, **NiceGUI**, **MariaDB** and **HashiCorp Vault**.

## What is currently included

- **Boot orchestration with state phases** (`waiting_core` → `loading_modules` → `ready`)
- **Vault-first secret handling** with auto-init/auto-unseal (optional via `LYNDRIX_MASTER_KEY`)
- **Persistent plugin lifecycle control** (install, enable/disable, reload, uninstall)
- **GitHub-based plugin installation** including version tags and safe ZIP extraction
- **Plugin dependencies with vendor folders** (`requirements.txt` → `vendor/` per plugin)
- **Automatic plugin reconciliation** via `LYNDRIX_PLUGINS_DESIRED`
- **Auth provider chain** (`local`, `ldap`, `oidc` + plugin providers via the bus)
- **API authentication** (system API key, user API keys, permission system + Swagger authorize)
- **Messaging gateway & notification router** (two-way messaging via provider adapters such as Discord; configurable notification routing)
- **Theming engine** (token/component themes, plugin-specific overrides)
- **Stable plugin API surface** via `core.api` (`__api_version__ = 1.2.0`)

## Quick start (development)

Prerequisites:

- Docker
- Docker Compose

```bash
git clone https://github.com/lyndrix-platform/lyndrix-core.git
cd lyndrix-core

# Optional: adjust docker/.env.dev

# Start the stack (App + DB + Vault + Docs)
docker compose -f docker/docker-compose.dev.yml up -d --build
```

Afterwards:

- App: `http://localhost:8081`
- Docs preview: `http://localhost:8000`

## Architecture (short)

- `app/main.py`: entry point, middleware, UI/FastAPI routing
- `app/core/bus.py`: global event bus (`subscribe`, `emit`, `create_tracked_task`)
- `app/core/components/vault/*`: Vault health, init/unseal, secret engine setup
- `app/core/components/database/*`: DB initialization and reconnect watchdog
- `app/core/components/plugins/*`: plugin manager, plugin service, UI

Details: [docs/architecture.md](docs/architecture.md)

## Plugin development (TL;DR)

1. Create a plugin folder under `/app/plugins/<plugin_name>`
2. Create `entrypoint.py` with `manifest` and `setup(ctx)`
3. Optional:
   - `render_settings_ui(ctx)`
   - `render_dashboard_widget(ctx)`
   - `teardown(ctx)`
   - `requirements.txt`
4. Install/update via the plugin manager or via `LYNDRIX_PLUGINS_DESIRED`

Complete guide: [docs/plugins.md](docs/plugins.md)

## Important configuration

The following are relevant in `app/config.py`, among others:

- `VAULT_URL`, `LYNDRIX_MASTER_KEY`
- `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `LYNDRIX_PLUGINS_DESIRED`, `LYNDRIX_PLUGINS_AUTO_UPDATE`
- `LYNDRIX_AUTH_PROVIDERS`
- LDAP/OIDC parameters (`LYNDRIX_LDAP_*`, `LYNDRIX_OIDC_*`)

## Documentation

- [Docs home](docs/index.md)
- [Deployment](docs/deployment.md)
- [Plugin Development](docs/plugins.md)
- [Security](docs/security.md)
- [Architecture](docs/architecture.md)

## License

MIT – see [LICENSE](LICENSE).
