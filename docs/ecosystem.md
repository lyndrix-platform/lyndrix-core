# Plugin Ecosystem

Lyndrix ships a set of first-party plugins built on the stable [plugin API](plugins.md). Each plugin lives in its own repository and has its own documentation site.

> **Documentation subdomains are provisioned per plugin** at `https://<slug>.docs.lyndrix.eu`. Until the DNS records and GitHub Pages custom domains are configured, these links may not resolve yet — the GitHub repository is always the source of truth.

## Shipped plugins

| Plugin | What it does | Docs | Source |
|---|---|---|---|
| **IaC Orchestrator** | GitOps controller that runs Terraform and Ansible pipelines, with webhook triggers, drift detection, and parallel provisioning. | [iac-orchestrator.docs.lyndrix.eu](https://iac-orchestrator.docs.lyndrix.eu) | [GitHub](https://github.com/lyndrix-platform/lyndrix-plugin-iac-orchestrator) |
| **Server Manager** | Central server inventory with configurable hardware catalogs, combination rules, and event-bus hooks for downstream order workflows. | [server-manager.docs.lyndrix.eu](https://server-manager.docs.lyndrix.eu) | [GitHub](https://github.com/lyndrix-platform/lyndrix-plugin-server-manager) |
| **Docker Manager** | Docker monitoring and runtime control (start/stop/restart/logs/shell) via the core sockets layer, with a dashboard widget. | [docker-manager.docs.lyndrix.eu](https://docker-manager.docs.lyndrix.eu) | [GitHub](https://github.com/lyndrix-platform/lyndrix-plugin-docker-manager) |
| **State Monitoring** | Native infrastructure and service monitoring with passive result ingestion, admin overrides, and inventory sync from other plugins. | [monitoring.docs.lyndrix.eu](https://monitoring.docs.lyndrix.eu) | [GitHub](https://github.com/lyndrix-platform/lyndrix-plugin-monitoring) |
| **External Services** | Embed external web UIs (Home Assistant, Grafana, …) as iframes through a managed service registry. | [external-services.docs.lyndrix.eu](https://external-services.docs.lyndrix.eu) | [GitHub](https://github.com/lyndrix-platform/lyndrix-plugin-external-services) |
| **Discord Notifier** | Two-way Discord integration (bot API + webhook) implemented as a [Messaging Gateway](core-components/messaging.md) adapter; supports multiple channel instances. | [discord-notifier.docs.lyndrix.eu](https://discord-notifier.docs.lyndrix.eu) | [GitHub](https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier) |
| **Meeting Bingo** | Multiplayer bullshit-bingo for long meetings — a reference/novelty plugin. | [meeting-bingo.docs.lyndrix.eu](https://meeting-bingo.docs.lyndrix.eu) | [GitHub](https://github.com/lyndrix-platform/lyndrix-plugin-meeting-bingo) |

## Discovering and installing plugins

The marketplace reads its catalog from the **plugin collection** registry, configurable via `LYNDRIX_PLUGIN_COLLECTION_URL` (see [Installation](deployment.md)). Browse the full registry at the
[lyndrix-plugin-collection](https://github.com/lyndrix-platform/lyndrix-plugin-collection) repository.

Install a plugin from the Plugin Manager UI, or declare it for reconciliation on boot via `LYNDRIX_PLUGINS_DESIRED` (see the [Plugin Development Guide](plugins.md#desired-plugin-state)).

## Building your own

See the [Plugin Development Guide](plugins.md) for the full plugin contract, and the per-component references for the extension points these plugins build on:

- [Messaging Gateway](core-components/messaging.md) — provider adapters (Discord, Slack, …)
- [Notification Router](core-components/notification-router.md) — declared notification endpoints
- [Sockets](core-components/sockets.md) — custom socket providers
- [Settings](core-components/settings.md#theming) — plugin-scoped theme overrides
