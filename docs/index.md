# Lyndrix Core Dokumentation

Willkommen zur technischen Dokumentation von Lyndrix Core.

Diese Doku wurde auf den aktuellen Core-Stand gebracht und enthält den vollständigen Plugin-Developer-Fokus inklusive API, Lifecycle und Core-Integrationspunkten.

## Schnellnavigation

- [Installation & Deployment](deployment.md)
- [Plugin Development Guide](plugins.md)
- [Security & Vault](security.md)
- [System-Architektur](architecture.md)

## Plattformüberblick

Lyndrix Core kombiniert:

- **FastAPI + NiceGUI** für API und UI
- **Globalen Event-Bus** für lose gekoppelte Modul-Kommunikation
- **Vault-Integration** für Secrets und verschlüsselte Vault-Key-Persistenz
- **Persistente Plugin-Zustände** (DB-basiert) für Boot-Restore und Lifecycle

## Wichtigste Neuerungen im aktuellen Stand

- Plugin-Manifeste unterstützen zusätzlich:
  - `dependencies`
  - `min_core_version`
  - `auto_enable_on_install`
  - `repo_url`
- Plugin-Service bietet:
  - GitHub Tag-Versionierung
  - sichere ZIP-Extraktion (Path-Traversal-Check)
  - atomisches Upgrade via Staging/Swap
  - lokale Vendor-Dependency-Installation pro Plugin
- Module-Manager unterstützt:
  - Dependency-Blocking (`status=blocked`)
  - Reconciliation gewünschter Plugins über `LYNDRIX_PLUGINS_DESIRED`
  - Reload/Unload/Toggle inkl. UI-Refresh-Events

## Für wen welche Seite?

- **Admins / Betreiber**: zuerst [deployment.md](deployment.md), dann [security.md](security.md)
- **Plugin-Entwickler**: direkt [plugins.md](plugins.md)
- **Core-Entwickler**: zusätzlich [architecture.md](architecture.md)
