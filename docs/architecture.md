# System-Architektur

## Überblick

Lyndrix Core ist event-getrieben aufgebaut. Zentrale Dienste initialisieren sich über den globalen Event-Bus und geben Zustände über klar benannte Topics weiter.

## Hauptbausteine

- `main.py`
  - FastAPI-App + NiceGUI-Integration
  - Boot-Interceptor-Middleware
- `core/bus.py`
  - Topic-basierte Event-Verteilung
  - Task-Tracking mit Fehler-Logging
- `core/services.py`
  - zentraler Service-Facade-Import
- `core/components/vault/*`
  - Vault Health, Init, Unseal, Mount-Setup
- `core/components/database/*`
  - DB-Verbindung + Reconnect-Watchdog
- `core/components/plugins/*`
  - Modul-Discovery, Lifecycle, Marketplace, Installation
- `core/components/auth/*`
  - Provider-Registry, Local/LDAP/OIDC, Provider-Chain

## Boot-Sequenz (vereinfacht)

1. `system:started` (App-Startup)
2. Vault prüft Zustand
3. Bei offenem Vault: `vault:opened`
4. DB initiiert Verbindung auf `vault:opened`
5. Bei DB bereit: `db:connected`
6. Auth-Service signalisiert `iam:ready`
7. BootService lädt Module/Plugins
8. `system:boot_complete`

## Plugin-Architektur

### ModuleManager

Aufgaben:

- Scan von Core-Modulen und Plugins
- Manifest-Validierung
- Setup-Aufruf (sync/async)
- Persistenz von Plugin-Zuständen (`PluginState`)
- Dependency-gesteuerte Aktivierung
- Reload/Unload/Toggle

### PluginService

Aufgaben:

- Installation aus GitHub ZIP (Branch/Tag)
- sichere Extraktion + atomische Verzeichnis-Swaps
- Abhängigkeitsinstallation in `vendor/`
- Marketplace-Fetch mit Cache und Fallback

## Auth-Architektur

- Provider-Registry verwaltet Provider in Reihenfolge
- Provider-Chain aus `LYNDRIX_AUTH_PROVIDERS`
- lokale Fallback-Authentifizierung schützt Admin-Zugang
- Plugins können eigene Provider per Bus registrieren

## Datenhaltung

- MariaDB via SQLAlchemy 2.x (`mysql+pymysql`)
- Vault als Secret Source-of-Truth
- Plugin-Lifecycle-Status in `plugin_states`

## Betriebsereignisse (Beispiele)

- System: `system:boot_phase`, `system:boot_complete`, `system:maintenance_mode`
- Vault: `vault:needs_init`, `vault:needs_unseal`, `vault:ready_for_data`
- Plugin: `plugin:install_started`, `plugin:installed`, `plugin:files_changed`
- Git/Collection: `git:sync`, `git:status_update`
