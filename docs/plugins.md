# Plugin Development Guide

Diese Seite dokumentiert vollständig, wie eigene Plugins für Lyndrix Core entwickelt werden und welche Core-Funktionen dafür bereitstehen.

## 1) Grundprinzip

Ein Plugin ist ein Python-Paket unter `/app/plugins/<plugin_folder>` mit einem `entrypoint.py`, das mindestens ein `manifest` bereitstellt und über `setup(ctx)` in den Core integriert wird.

## 2) Verzeichnisstruktur

```text
app/plugins/my_plugin/
├── entrypoint.py
├── requirements.txt        # optional
├── vendor/                 # wird bei Installation erzeugt (optional)
├── assets/                 # optional
└── locales/                # optional, wird automatisch registriert
```

Wichtig:

- Ordnername muss Python-kompatibel sein (`isidentifier()`)
- Bindestriche werden beim Laden automatisch zu Unterstrichen normalisiert

## 3) Stabile API verwenden

Für Plugin-Code bevorzugt aus `core.api` importieren:

```python
from core.api import __api_version__, ModuleManifest
```

Aktuell:

- `__api_version__ = "1.0.0"`

Das reduziert Abhängigkeit auf interne Pfade.

## 4) Manifest vollständig

`ModuleManifest` (Pydantic) unterstützt:

- `id` *(required)*: eindeutige ID, empfohlen `lyndrix.plugin.<name>`
- `name` *(required)*
- `version` *(required)*
- `description`, `author`, `icon`
- `type`: `PLUGIN` oder `CORE` (für eigene Plugins: `PLUGIN`)
- `ui_route`: Route für Navigation/Seite
- `permissions.subscribe`: erlaubte Event-Topics
- `permissions.emit`: erlaubte Event-Topics
- `permissions.vault_paths`: zusätzliche Vault-Pfade
- `settings_schema`: frei nutzbar
- `dependencies`: Liste abhängiger Module
- `min_core_version`: minimale Core-API-Version
- `auto_enable_on_install`: Default-Aktivierungszustand
- `repo_url`: Quell-Repository (für Marketplace/Updates)

## 5) Lifecycle-Hooks

### Pflicht

- `setup(ctx)` (sync oder async)

### Optional

- `teardown(ctx)` bei Deaktivierung
- `render_settings_ui(ctx)` für Settings-Dialog
- `render_dashboard_widget(ctx)` für Dashboard-Karte

Module-Status intern:

- `initializing`
- `active`
- `disabled`
- `blocked` (Dependencies nicht erfüllt)

## 6) ModuleContext (`ctx`) – was der Core bereitstellt

`ctx` stellt pro Plugin bereit:

- `ctx.manifest`: Manifest-Objekt
- `ctx.log`: Plugin-spezifischer Logger
- `ctx.state`: flüchtiger In-Memory-Zustand
- `ctx.subscribe(topic)`: Event-Subscription (Permission-checked)
- `ctx.emit(topic, payload)`: Event-Emission (Permission-checked)
- `ctx.create_task(coro, name=...)`: getrackte Async-Tasks
- `ctx.get_secret(key)`: Secret lesen (Vault KV v2)
- `ctx.set_secret(key, value)`: Secret schreiben (Read-Modify-Write mit Lock)

Vault-Isolation:

- Core-Module: `core/<manifest.id>`
- Plugins: `plugins/<manifest.id>`
- Mountpoint: `lyndrix` (KV v2)

## 7) Event-System für Plugins

Relevante systemische Topics:

- `vault:ready_for_data`
- `system:boot_complete`
- `db:connected`
- `plugin:install_started`
- `plugin:installed`
- `plugin:install_failed`
- `plugin:files_changed`
- `git:status_update`
- `ui:needs_refresh`

Wichtig:

- Nur Events aus `permissions.subscribe` sind abonnierbar
- Nur Events aus `permissions.emit` sind sendbar
- Fehler werden geloggt, unerlaubte Operationen werden blockiert

## 8) Dependencies im Plugin

Wenn `requirements.txt` existiert, installiert der Plugin-Service Pakete nach `vendor/` im Plugin-Ordner.

Hinweise:

- Installation erfolgt während Plugin-Install/Upgrade
- Timeout/Fehler führen zu Abbruch und Cleanup
- Verdächtige Requirement-Zeilen (z. B. lokale Pfade) werden geblockt

## 9) Installation, Update, Versionierung

Der Core kann Plugins von GitHub installieren:

- Quelle: Repository-URL
- Version: `latest` (Default-Branch ZIP) oder Tag (`v1.2.3`)
- Upgrade: atomischer Swap mit Backup-Fallback

Zusätzlich:

- Tag-Liste wird via GitHub API geladen und gecacht
- Marketplace-Daten kommen primär aus `lyndrix-plugin-collection` (lokaler Clone) oder HTTP-Fallback

## 10) Desired Plugins (automatische Soll-Konfiguration)

Über `LYNDRIX_PLUGINS_DESIRED` können Plugins als gewünschter Zustand definiert werden.

Format:

```text
https://github.com/org/plugin-a@v1.2.0,https://github.com/org/plugin-b
```

Optionen:

- ohne `@version` ⇒ `latest`
- `LYNDRIX_PLUGINS_AUTO_UPDATE=true` aktualisiert `latest`-Plugins beim Reconcile

## 11) Plugin kann Auth-Provider registrieren

Plugins können eigene Auth-Provider zur Laufzeit registrieren über Event:

- `auth:register_provider` mit Payload `{"provider": <AuthProvider instance>}`

Dadurch lässt sich die Login-Kette (`LYNDRIX_AUTH_PROVIDERS`) um eigene Provider erweitern.

## 12) Minimales Plugin-Beispiel

```python
from core.api import ModuleManifest
from nicegui import ui

manifest = ModuleManifest(
    id="lyndrix.plugin.hello",
    name="Hello Plugin",
    version="1.0.0",
    description="Example plugin",
    author="Example",
    type="PLUGIN",
    ui_route="/hello",
    permissions={"subscribe": ["system:boot_complete"], "emit": []},
)

async def setup(ctx):
    @ui.page('/hello')
    async def hello_page():
        ui.label('Hello from plugin')
```

## 13) Best Practices

- Secrets ausschließlich mit `ctx.get_secret`/`ctx.set_secret`
- I/O-lastige Arbeit in `ctx.create_task(...)` auslagern
- `manifest.id` stabil halten (State-Migration)
- Bei inkompatiblen Core-Änderungen `min_core_version` setzen
- Plugin-README + Changelog im Plugin-Repo pflegen

## 14) Troubleshooting

- Plugin lädt nicht:
  - `entrypoint.py` vorhanden?
  - `manifest` gültig?
  - Ordnername Python-kompatibel?
- Plugin blockiert:
  - Abhängigkeiten in `dependencies` aktiv?
- Secret-Zugriff leer:
  - Vault offen?
  - Event `vault:ready_for_data` abgewartet?
- Update klappt nicht:
  - Tag existiert?
  - `requirements.txt` gültig?
