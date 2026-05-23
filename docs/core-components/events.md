# Event Catalog

This catalog lists event topics currently used in Lyndrix Core (publish and/or subscribe in source code).

## Lifecycle and system

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `system:started` | `main.py` startup | Vault, Monitor | Global startup trigger |
| `system:boot_phase` | Boot | UI (boot screen) | Phase updates: waiting/loading/ready/failed |
| `system:boot_complete` | Boot | Plugin component | Signals module loading completion |
| `system:maintenance_mode` | Boot, Database | Maintenance UI | Activates/deactivates maintenance overlay |
| `system:metrics_update` | System Monitor | UI metrics consumers | High-frequency metrics event |

## Vault

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `vault:init_requested` | Vault UI, Auto-Unseal | Vault service | Init flow trigger |
| `vault:unseal_requested` | Vault UI, Auto-Unseal | Vault service | Unseal flow trigger |
| `vault:needs_init` | Vault service | Auto-Unseal | Vault not initialized |
| `vault:needs_unseal` | Vault service | Auto-Unseal | Vault sealed |
| `vault:auth_failed` | Vault service | (none in core) | Token/permission failure |
| `vault:opened` | Vault service | Database | Vault ready to unlock DB init |
| `vault:ready_for_data` | Vault service | Plugins (recommended) | Safe point for plugin secret usage |
| `vault:unseal_failed` | (reserved) | Vault unseal UI | UI subscribes; no active emitter in core |

## Database and IAM

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `db:connected` | Database | Auth, ModuleManager fallback | DB ready signal |
| `iam:ready` | Auth | Boot | IAM/provider chain ready |
| `auth:register_provider` | Plugin/Auth providers | Auth provider registry | Runtime custom provider registration |

## Plugin lifecycle and UI refresh

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `plugin:install_started` | Plugin service | (none in core) | Install progress hook |
| `plugin:installed` | Plugin service | (none in core) | Install success hook |
| `plugin:install_failed` | Plugin service | (none in core) | Install failure hook |
| `plugin:files_changed` | Plugin service | ModuleManager | Triggers load/reload/unload |
| `ui:needs_refresh` | ModuleManager | Main layout refresh hook | Forces UI re-render |

## Git integration

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `git:sync` | Plugin service | Git service | Clone/pull trigger |
| `git:commit_push` | Plugins/UI integrations | Git service | Commit/push trigger |
| `git:status_update` | Git service | Plugin service | Sync/commit status updates |

## Notifications

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `system:notify` | Core/plugins | Notification component | Broadcast notifications |
| `user:notify` | Core/plugins | Notification component | User-targeted notifications |
| `notification:outbound` | Notification component | External listeners/plugins | Outbound normalized event |

## Monitoring-specific (summarized in event-bus logging)

| Topic | Notes |
|---|---|
| `monitoring:inventory_sync` | Payload logging is summarized by Event Bus |
| `monitoring:state_changed` | Payload logging is summarized by Event Bus |

## For plugin authors

- Subscribe and emit via `ctx.subscribe(...)` / `ctx.emit(...)`
- Declare every used topic in manifest permissions
- Prefer stable existing topics before creating custom ones
