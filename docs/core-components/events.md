# Event Catalog

This catalog summarizes the event topics currently used in Lyndrix Core.

## Lifecycle and system

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `system:started` | `main.py` startup | Vault, System Monitoring | Initial runtime trigger |
| `system:boot_phase` | Boot | UI and boot-state consumers | Phase updates such as `waiting_core`, `loading_modules`, `ready`, `failed` |
| `system:boot_complete` | Boot | Plugins component and plugins | Signals that core startup completed |
| `system:maintenance_mode` | Boot, Database | Maintenance UI | Enables or clears maintenance mode |
| `system:metrics_update` | System Monitoring | UI metrics consumers | Periodic host metrics update |

## Vault

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `vault:init_requested` | Vault UI, auto-unseal flow | Vault service | Trigger Vault initialization |
| `vault:unseal_requested` | Vault UI, auto-unseal flow | Vault service | Trigger Vault unseal |
| `vault:needs_init` | Vault service | Auto-unseal manager, setup UI | Vault is not initialized |
| `vault:needs_unseal` | Vault service | Auto-unseal manager, unseal UI | Vault is sealed |
| `vault:auth_failed` | Vault service | Operational consumers | Token or policy issue while preparing the mount |
| `vault:opened` | Vault service | Database | Vault is available for downstream startup |
| `vault:ready_for_data` | Vault service | Plugins and other secret consumers | Safe point for reading and writing secret data |
| `vault:unseal_failed` | Reserved topic | Unseal UI | UI subscribes, no active emitter in current core |

## Database and IAM

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `db:connected` | Database | Auth, Plugins fallback activation | Database is ready for schema work and state restore |
| `iam:ready` | Auth | Boot | IAM bootstrap and provider-chain initialization finished |
| `auth:register_provider` | Plugins or auth code | Auth provider registry | Registers runtime auth providers |

## Plugin lifecycle and UI refresh

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `plugin:install_started` | Plugin service | Optional listeners | Start of a plugin install or upgrade |
| `plugin:installed` | Plugin service | Optional listeners | Plugin install succeeded |
| `plugin:install_failed` | Plugin service | Optional listeners | Plugin install failed |
| `plugin:files_changed` | Plugin service | ModuleManager | Triggers load, reload, or unload handling |
| `ui:needs_refresh` | ModuleManager | Layout/UI refresh hooks | Requests a UI rebuild after module changes |

## Git integration

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `git:sync` | Plugin service and integrations | Git service | Clone or pull a repository |
| `git:commit_push` | Plugins or UI integrations | Git service | Stage, commit, and push repo changes |
| `git:status_update` | Git service | Plugin service | Reports sync or commit result state |

## Notifications

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `system:notify` | Core and plugins | Notifications component | Broadcast notification |
| `user:notify` | Core and plugins | Notifications component | User-targeted notification |
| `notification:outbound` | Notifications component | Messaging gateway (legacy bridge), external listeners | Normalized outbound notification stream; bridged into `messaging:outbound` for backward compatibility |
| `notification:routed` | Plugins via `ctx.notify(...)` | Notification Router | A `NotificationEnvelope` for a declared endpoint; routed internally and/or externally |

## Messaging gateway

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `messaging:outbound` | Core and plugins | Messaging Gateway | An `OutboundMessage`; dispatched to a target provider or broadcast to all adapters |
| `messaging:action_response` | Messaging Gateway | Plugins | Declared for interactive button round-trips |
| `<plugin>:action_response` | Messaging Gateway | Originating plugin | Resume topic emitted when an inbound correlation resolves (name derived from `source_plugin_id`) |

## Sockets

| Topic | Produced by | Consumed by | Notes |
|---|---|---|---|
| `socket:request` | Core and plugins | Sockets component | Run an operation (`docker:health`, `docker:spawn`, …) against a named provider |
| `socket:response` | Sockets component | Requesters | Result of a `socket:request` |
| `socket:provider:register` | Plugins | Sockets component | Register a plugin-supplied `BaseSocketProvider` |

## Monitoring-specific topics summarized in logs

| Topic | Notes |
|---|---|
| `monitoring:inventory_sync` | Payload logging is summarized by the event bus |
| `monitoring:state_changed` | Payload logging is summarized by the event bus |

## Guidance for plugin authors

- subscribe and emit through `ctx.subscribe(...)` and `ctx.emit(...)`
- declare every used topic in the manifest permissions
- prefer existing stable topics before inventing new ones
- avoid putting secrets or oversized blobs into event payloads
