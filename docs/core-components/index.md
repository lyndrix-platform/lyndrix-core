# Core Components Reference

This section documents the internal core components of Lyndrix Core, together with the event bus and the event catalog.

## Included pages

- [Event Bus](event-bus.md)
- [Event Catalog](events.md)
- [Auth Component](auth.md)
- [Boot Component](boot.md)
- [Dashboard Component](dashboard.md)
- [Database Component](database.md)
- [Git Component](git.md)
- [Messaging Gateway Component](messaging.md)
- [Notifications Component](notifications.md)
- [Notification Router Component](notification-router.md)
- [Plugins Component](plugins.md)
- [Settings Component](settings.md)
- [Sockets Component](sockets.md)
- [System Monitoring Component](system.md)
- [Vault Component](vault.md)

## Component map

| Component | Responsibility |
|---|---|
| Auth | IAM bootstrap, provider-chain initialization, user and group persistence |
| Boot | Boot phase state machine and module-loading release point |
| Dashboard | Main authenticated landing page and widget host |
| Database | SQLAlchemy engine lifecycle, connection retries, readiness signaling |
| Git | Repository clone/pull/commit workflows triggered by events |
| Messaging Gateway | Two-way messaging registry; plugin provider adapters, dispatch, retries, correlation, streaming |
| Notifications | Notification persistence, UI toasts, and webhook ingestion |
| Notification Router | Routes plugin notification endpoints to internal UI and/or external providers via precedence resolution |
| Plugins | Discovery, lifecycle, install/update/uninstall, marketplace integration |
| Settings | Runtime configuration UI and the theming engine |
| Sockets | Socket-provider registry (Docker, …); mount inspection and permission repair behind core auth |
| System Monitoring | Periodic CPU, RAM, and disk metrics emission |
| Vault | Vault health checks, init/unseal handling, secret-store readiness |

## How to use this section

Use these pages when you need to answer questions such as:

- which event starts a component
- which files implement a specific runtime capability
- which component owns a route or a lifecycle hook
- where to extend Lyndrix without breaking the plugin API

For system-wide flow, start with [../architecture.md](../architecture.md). For topic-level behavior, follow with [events.md](events.md).
