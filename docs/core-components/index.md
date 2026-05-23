# Core Components

This section documents every internal core component of Lyndrix Core, plus the Event Bus and all currently available event topics.

## Included documentation

- [Event Bus](event-bus.md)
- [Event Catalog](events.md)
- [Auth Component](auth.md)
- [Boot Component](boot.md)
- [Dashboard Component](dashboard.md)
- [Database Component](database.md)
- [Git Component](git.md)
- [Notifications Component](notifications.md)
- [Plugins Component](plugins.md)
- [Settings Component](settings.md)
- [System Monitoring Component](system.md)
- [Vault Component](vault.md)

## Component map (quick overview)

| Component | Responsibility |
|---|---|
| Auth | Identity, provider chain, user/group bootstrap, IAM readiness |
| Boot | Boot phase state machine and module loading trigger |
| Dashboard | Main runtime dashboard UI route |
| Database | SQLAlchemy engine lifecycle, reconnect loop, DB readiness event |
| Git | Event-driven git clone/sync/commit workflows |
| Notifications | In-app + webhook-driven notifications and outbound event publishing |
| Plugins | Module discovery, plugin lifecycle, install/update/uninstall, marketplace sync |
| Settings | Settings UI route for runtime configuration |
| System Monitoring | Runtime CPU/RAM/disk metrics emission |
| Vault | Vault health/init/unseal flow and secret-store readiness |
