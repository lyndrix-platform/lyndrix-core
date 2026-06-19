# Lyndrix Core

Lyndrix Core ist der zentrale Runtime-Core für modulare Internal-Platform-Workloads auf Basis von **FastAPI**, **NiceGUI**, **MariaDB** und **HashiCorp Vault**.

## Was aktuell drin ist

- **Boot-Orchestrierung mit Zustandsphasen** (`waiting_core` → `loading_modules` → `ready`)
- **Vault-first Secret Handling** mit Auto-Init/Auto-Unseal (optional über `LYNDRIX_MASTER_KEY`)
- **Persistente Plugin-Lifecycle-Steuerung** (Install, Aktivieren/Deaktivieren, Reload, Uninstall)
- **GitHub-basierte Plugin-Installation** inkl. Versionstags und sicherer ZIP-Extraktion
- **Plugin-Abhängigkeiten mit Vendor-Ordnern** (`requirements.txt` → `vendor/` pro Plugin)
- **Automatische Plugin-Reconciliation** über `LYNDRIX_PLUGINS_DESIRED`
- **Auth-Provider-Chain** (`local`, `ldap`, `oidc` + Plugin-Provider via Bus)
- **API-Authentifizierung** (System-API-Key, User-API-Keys, Permission-System + Swagger-Authorize)
- **Messaging Gateway & Notification Router** (Zwei-Wege-Messaging über Provider-Adapter wie Discord; konfigurierbares Notification-Routing)
- **Theming-Engine** (Token-/Component-Themes, Plugin-spezifische Overrides)
- **Stabile Plugin-API-Oberfläche** über `core.api` (`__api_version__ = 1.2.0`)

## Schnellstart (Development)

Voraussetzungen:

- Docker
- Docker Compose

```bash
git clone https://github.com/lyndrix-platform/lyndrix-core.git
cd lyndrix-core

# Optional: docker/.env.dev anpassen

# Stack starten (App + DB + Vault + Docs)
docker compose -f docker/docker-compose.dev.yml up -d --build
```

Danach:

- App: `http://localhost:8081`
- Docs-Preview: `http://localhost:8000`

## Architektur (Kurz)

- `app/main.py`: Entry-Point, Middleware, UI/FastAPI-Routing
- `app/core/bus.py`: globaler Event-Bus (`subscribe`, `emit`, `create_tracked_task`)
- `app/core/components/vault/*`: Vault-Health, Init/Unseal, Secret-Engine-Setup
- `app/core/components/database/*`: DB-Initialisierung und Reconnect-Watchdog
- `app/core/components/plugins/*`: Plugin-Manager, Plugin-Service, UI

Detailliert: [docs/architecture.md](docs/architecture.md)

## Plugin-Entwicklung (TL;DR)

1. Plugin-Ordner unter `/app/plugins/<plugin_name>` anlegen
2. `entrypoint.py` mit `manifest` und `setup(ctx)` erstellen
3. Optional:
   - `render_settings_ui(ctx)`
   - `render_dashboard_widget(ctx)`
   - `teardown(ctx)`
   - `requirements.txt`
4. Über Plugin-Manager installieren/aktualisieren oder via `LYNDRIX_PLUGINS_DESIRED`

Komplette Anleitung: [docs/plugins.md](docs/plugins.md)

## Wichtige Konfiguration

In `app/config.py` sind u. a. relevant:

- `VAULT_URL`, `LYNDRIX_MASTER_KEY`
- `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `LYNDRIX_PLUGINS_DESIRED`, `LYNDRIX_PLUGINS_AUTO_UPDATE`
- `LYNDRIX_AUTH_PROVIDERS`
- LDAP/OIDC Parameter (`LYNDRIX_LDAP_*`, `LYNDRIX_OIDC_*`)

## Dokumentation

- [Docs Startseite](docs/index.md)
- [Deployment](docs/deployment.md)
- [Plugin Development](docs/plugins.md)
- [Security](docs/security.md)
- [Architektur](docs/architecture.md)

## Lizenz

MIT – siehe [LICENSE](LICENSE).
