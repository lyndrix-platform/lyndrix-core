# Lyndrix Core — Dokumentation

> Version 0.4.0 · „First Light Alpha"

Lyndrix Core ist das Python-Runtime-Framework hinter der Lyndrix-Plattform. Es kombiniert **FastAPI** (REST-Backend) und **NiceGUI** (optionales In-Process-Frontend) zu einem erweiterbaren, event-getriebenen Monolith, der durch Plugins um beliebige Funktionalität ergänzt werden kann. Jedes Plugin erhält dieselben Plattformdienste — Authentifizierung, Datenbank, Secrets-Vault, Event-Bus, Theming und Internationalisierung — ohne eigene Infrastruktur aufbauen zu müssen.

---

## Inhaltsverzeichnis

1. [Übersicht](#übersicht)
2. [Architektur](#architektur)
3. [Konfiguration](#konfiguration)
4. [UI-Engine](#ui-engine)
5. [Authentifizierung](#authentifizierung)
6. [Vault-Integration](#vault-integration)
7. [Datenbank](#datenbank)
8. [Plugin-System](#plugin-system)
9. [Messaging-Gateway](#messaging-gateway)
10. [Internationalisierung](#internationalisierung)
11. [Theming](#theming)
12. [REST-API-Übersicht](#rest-api-übersicht)
13. [Entwicklung](#entwicklung)

---

## Übersicht

Lyndrix Core startet als einzelner Docker-Dienst und bootstrappt sich vollständig selbst: HashiCorp Vault wird initialisiert und entsperrt, die Datenbank wird verbunden, die IAM-Kette wird aufgebaut und schließlich werden alle konfigurierten Plugins geladen. Entwickler schreiben ausschließlich Plugin-Code — die gesamte Infrastruktur stellt Core bereit.

**Kernprinzipien:**

- **Plugin-First**: Jede Funktion außerhalb des Kerns wird als Plugin entwickelt. Core stellt eine stabile API-Oberfläche (`from core.api import …`) bereit, die sich nie ändert.
- **API-First**: Jede Plugin-Funktion ist über das REST-Backend erreichbar. Frontends (React oder NiceGUI) sind reine Konsumenten der API.
- **Event-getrieben**: Komponenten kommunizieren ausschließlich über den globalen Event-Bus (`bus.py`). Keine direkten Abhängigkeiten zwischen Plugins.
- **Zero-Trust-Secrets**: Alle Secrets werden in Vault gespeichert. Umgebungsvariablen können immer überschrieben werden, gewinnen aber immer gegen Vault-Werte.

---

## Architektur

### Boot-Sequenz

Der Start läuft in einer festen, event-gesteuerten Reihenfolge:

```
system:started
  └─► Vault: init/seal prüfen ──► vault:opened
        └─► Datenbank verbinden ──► db:connected
              └─► IAM-Kette aufbauen ──► iam:ready
                    └─► Core-Module + Plugins laden ──► system:boot_complete
```

Die Boot-Zustandsmaschine kennt die Zustände:

| Zustand | Bedeutung |
|---|---|
| `waiting_core` | Warten auf Vault und DB |
| `loading_modules` | Plugins werden initialisiert |
| `ready` | System vollständig gestartet |
| `failed` | Fataler Fehler während des Starts |

### Wichtige Quelldateien

| Datei | Aufgabe |
|---|---|
| `app/main.py` | FastAPI + NiceGUI Setup, Middleware, Route-Registrierung, Startup-Hook |
| `app/config.py` | `Settings` (pydantic-settings); Hierarchie: OS-Env > `.env` > Vault > Standard |
| `app/core/bus.py` | `GlobalEventBus` — `subscribe(topic)` Decorator + `emit(topic, payload)` |
| `app/core/services.py` | Einzelner Import-Punkt für alle Singleton-Dienste |
| `app/core/api/__init__.py` | Stabile Plugin-API-Oberfläche (`__api_version__ = "1.1.0"`) |
| `app/core/i18n.py` | Übersetzungsengine, `t()`, Katalog-Versionierung, `client_namespaces()` |

### Komponenten-Anatomie

Jede Core-Komponente und jedes Plugin folgt derselben kanonischen Struktur:

```
<komponente>/
├── model/        # SQLAlchemy-Modelle, Pydantic-Schemas, YAML-Loader
├── logic/        # Service-Singletons, CRUD, Event-Handler
├── api/          # FastAPI-Router (API-first — beide UIs nutzen ausschließlich die API)
└── ui/
    ├── react/    # React-Frontend (Vite IIFE-Bundle → app/ui/static/ui_bundle.js)
    └── nicegui/  # NiceGUI-Seiten und Widgets (opt-in)
```

### Event-Bus

```python
from core.api import event_bus

# Abonnieren (im Plugin-Setup):
@ctx.subscribe('db:connected')
async def on_db_ready(payload):
    await my_service.initialize()

# Senden:
await ctx.emit('myplugin:something_happened', {"key": "value"})
```

Plugins deklarieren abonnierte und gesendete Topics im Manifest unter `permissions`. Der Bus garantiert keine Reihenfolge zwischen unabhängigen Abonnenten.

### Plattform-Events

| Topic | Zeitpunkt |
|---|---|
| `vault:ready_for_data` | Vault entsperrt, Secrets sicher lesbar/schreibbar |
| `db:connected` | Datenbank bereit (wird auch bei Reconnect erneut gefeuert) |
| `system:boot_complete` | Alle Core-Komponenten und Plugins vollständig gestartet |
| `ui:needs_refresh` | Frontend soll seinen State neu laden |
| `messaging:outbound` | Plugin möchte eine ausgehende Nachricht versenden |

---

## Konfiguration

Alle Einstellungen werden über die `Settings`-Klasse in `app/config.py` verwaltet. Die Auflösungsreihenfolge ist:

```
OS-Umgebungsvariable  >  .env-Datei  >  Vault KV  >  Standardwert
```

OS-Umgebungsvariablen **gewinnen immer** — sie werden nach der Vault-Hydration nicht überschrieben.

### Kernvariablen

| Variable | Standard | Bedeutung |
|---|---|---|
| `VAULT_URL` | `http://vault:8200` | Adresse des HashiCorp Vault |
| `LYNDRIX_MASTER_KEY` | — | Optionaler Auto-Unseal-Key |
| `DB_HOST` | `db` | MariaDB-Hostname |
| `DB_PORT` | `3306` | MariaDB-Port |
| `DB_NAME` | `lyndrix` | Datenbankname |
| `DB_USER` | `lyndrix` | Datenbankbenutzer |
| `DB_PASSWORD` | — | Datenbankpasswort |
| `LYNDRIX_AUTH_PROVIDERS` | `local` | Geordnete, kommagetrennte Auth-Kette: `local`, `ldap`, `oidc` |
| `LYNDRIX_PLUGINS_DESIRED` | — | Kommagetrennte Plugin-Specs die beim Start reconciliert werden |
| `LYNDRIX_PLUGINS_AUTO_UPDATE` | `false` | `latest`-Plugins beim Neustart automatisch aktualisieren |
| `LYNDRIX_SYSTEM_API_KEY` | — | Master-M2M-API-Key (deaktiviert wenn nicht gesetzt) |
| `LYNDRIX_UI_ENGINE` | `api` | Aktive UI-Engines, kommagetrennt: `api`, `react`, `nicegui` |
| `DEFAULT_LOCALE` | `de` | Standard-Sprache der Plattform |
| `DEFAULT_THEME_ID` | `default` | ID des aktiven Themes |
| `SECRET_KEY` | — | JWT-Signing-Key |
| `CORS_ORIGINS` | `*` | Erlaubte CORS-Ursprünge (kommagetrennt) |

### Vault-Hydration

Beim Empfang des `vault:opened`-Events ruft Core `settings.hydrate_from_vault()` auf. Dabei werden KV-Werte aus dem `lyndrix`-Mount gelesen und in `Settings` übernommen — **sofern die zugehörige Umgebungsvariable nicht bereits gesetzt ist**.

---

## UI-Engine

Ab v0.4.0 ist `LYNDRIX_UI_ENGINE` ein **kommagetrennte Mehrfachwert-Liste**. NiceGUI ist nicht mehr standardmäßig aktiv.

| Wert | Bedeutung |
|---|---|
| `api` | Nur REST-API, keine UI gestartet (Standard, geeignet für M2M-Deployments) |
| `react` | `lyndrix-ui` React-SPA wird als statisches Bundle ausgeliefert |
| `nicegui` | In-Process NiceGUI-Frontend wird aktiviert (opt-in) |

Beispiele:

```bash
# Nur REST-API (Standard):
LYNDRIX_UI_ENGINE=api

# React-UI aktivieren:
LYNDRIX_UI_ENGINE=api,react

# React-UI + NiceGUI parallel (Entwicklung / Migration):
LYNDRIX_UI_ENGINE=api,react,nicegui
```

Die React-UI (`lyndrix-ui`) kommuniziert ausschließlich über relative `/api/…`-Pfade. Das Bundle wird von Core unter `/` ausgeliefert und benötigt keinen separaten Server. Plugins liefern ihr eigenes React-Bundle als `app/ui/static/ui_bundle.js` — Core mountet es unter `/api/plugins/<id>/static/`.

---

## Authentifizierung

### Provider-Kette

`LYNDRIX_AUTH_PROVIDERS` definiert die Reihenfolge, in der Authentifizierungsversuche ausgewertet werden. Jeder Eintrag entspricht einem Provider:

| Provider | Anforderungen |
|---|---|
| `local` | Benutzerdaten in der lokalen MariaDB (Standard) |
| `ldap` | LDAP/Active-Directory-Server (Verbindungsdaten in Vault) |
| `oidc` | OpenID Connect (Client-ID/Secret in Vault) |

Der erste Provider, der den Nutzer kennt, „gewinnt". Unbekannte Nutzer werden an den nächsten Provider weitergegeben.

### API-Keys

Jeder Benutzer kann mehrere API-Keys mit fein granularen Berechtigungen anlegen:

```
POST /api/auth/keys          → Key erstellen
GET  /api/auth/keys          → eigene Keys auflisten
DELETE /api/auth/keys/{id}   → Key löschen
```

Plugins schützen ihre Endpunkte mit:

```python
from core.api import require_permission

@router.get("/data")
async def get_data(identity = Depends(require_permission("api:read"))):
    ...
```

### System-API-Key

`LYNDRIX_SYSTEM_API_KEY` ist ein unveränderlicher Master-Key für M2M-Zugriff (CI/CD, IaC-Pipeline). Er ist keinem Benutzer zugeordnet und sollte **nicht** für interaktive Zugänge genutzt werden.

---

## Vault-Integration

Lyndrix verwendet HashiCorp Vault als einzigen Secrets-Speicher. Core bootstrappt Vault vollständig:

1. **Init**: Vault wird mit einem einzelnen Unseal-Key initialisiert, der verschlüsselt gespeichert wird.
2. **Unseal**: Beim Start wird Vault automatisch entsperrt (via `LYNDRIX_MASTER_KEY` oder manuell).
3. **KV v2 Mount**: Alle Secrets liegen im `lyndrix` KV-Mount.
4. **Health-Checks**: Core überwacht den Vault-Status kontinuierlich und feuert `vault:opened` / `vault:sealed` bei Zustandsänderungen.

Plugins greifen **niemals** direkt auf Vault zu. Sie nutzen:

```python
# Über Context (empfohlen):
await ctx.get_secret("my_secret_key")
await ctx.set_secret("my_secret_key", "value")

# Oder über den Vault-Service:
from core.api import vault_service
value = await vault_service.kv_get("my_key")
```

---

## Datenbank

Core verwendet **MariaDB** (MySQL-kompatibel) über **SQLAlchemy** (async). Die gemeinsame `Base`-Klasse wird von Core und allen Plugins geteilt:

```python
from core.api import db_instance, Base

class MyModel(Base):
    __tablename__ = "my_plugin_table"
    id = Column(Integer, primary_key=True)
    ...
```

Plugins registrieren ihre Modelle beim `db:connected`-Event:

```python
@ctx.subscribe('db:connected')
async def on_db(payload):
    Base.metadata.create_all(db_instance.engine)
```

**Wichtig:** Modelle, die nach dem ersten Start erneut geladen werden können (z.B. bei Plugin-Reload), müssen `extend_existing=True` in `__table_args__` setzen, um `InvalidRequestError` zu vermeiden.

---

## Plugin-System

### Manifest-Grundstruktur

```python
from core.api import ModuleManifest

manifest = ModuleManifest(
    id="lyndrix.plugin.<name>",    # Permanent — Änderung erzeugt neue Plugin-Identität
    name="Mein Plugin",
    version="1.0.0",               # Muss mit dem Git-Tag übereinstimmen
    type="PLUGIN",
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-<name>",
    ui_route="/meinplugin",
    react_ui=True,                 # React-Bundle vorhanden
    i18n_namespace="meinplugin",   # i18next-Namespace für das React-Frontend
    auto_enable_on_install=False,  # False wenn Konfiguration vor Erstzulassung nötig
    permissions={
        "subscribe": ["db:connected", "vault:ready_for_data"],
        "emit": ["meinplugin:etwas_passiert"],
    },
)
```

### Lifecycle

```python
def setup(ctx):
    """Synchroner Einstiegspunkt. Darf NICHT blockieren."""
    ctx.register_routes(build_plugin_router(my_service))
    
    @ctx.subscribe('db:connected')
    async def on_db(payload):
        await my_service.init_db()
    
    # Hintergrundaufgaben über create_task starten:
    ctx.create_task(my_service.run_background_loop(), name="meinplugin:loop")

def teardown(ctx):
    """Aufräumen beim Plugin-Deaktivieren."""
    ...
```

### ModuleManager

`app/core/components/plugins/logic/manager.py` verwaltet den kompletten Plugin-Lebenszyklus:

- **Erkennung**: Plugins werden als Python-Pakete im Plugin-Verzeichnis oder via `LYNDRIX_PLUGINS_DESIRED` geladen.
- **Installation**: `plugin_service.py` lädt Plugins von GitHub (via Git-Tag), installiert Abhängigkeiten in `vendor/`, und aktiviert das Plugin.
- **Upgrade**: Gleicher Prozess — neue Version wird heruntergeladen, alte ersetzt.
- **Registry**: Alle aktiven Plugins sind über `ModuleManager.get_all()` abfragbar.

### Plugin-API-Oberfläche

```python
from core.api import (
    ModuleManifest, ModuleContext, PluginHealthStatus,
    db_instance, Base,         # Datenbankzugang
    APIRouter,                 # FastAPI-Router (re-exportiert)
    event_bus,                 # Globaler Event-Bus
    notification_service,      # Plattform-Benachrichtigungen senden
    auth_service,              # IAM-Zugriff
    plugin_service,            # Plugins programmatisch installieren/upgraden
    UIStyles,                  # Theming-Klassen
    require_permission,        # Endpunkt-Absicherung
)
```

**Regel:** Plugins importieren ausschließlich aus `core.api`. Direktimporte aus `core.components.*` sind verboten und werden in Code-Reviews abgelehnt.

---

## Messaging-Gateway

Das Messaging-Gateway ermöglicht Plugins, Nachrichten über externe Provider (Discord, Slack, etc.) zu versenden und **Antworten** zurück an das auslösende Plugin zu routen.

### Kanonische Anatomie (ab v0.4.0)

```
app/core/components/messaging/
├── model/
│   ├── schemas.py          # Pydantic-Schemas: MessageSeverity, ActionButton,
│   │                       # OutboundMessage, InboundMessage, DeliveryResult
│   └── pending_action.py   # SQLAlchemy PendingActionRecord (Korrelations-Tracking)
└── logic/
    ├── adapter.py          # GatewayAdapter ABC, GatewayCapability, ProviderConfigField
    ├── correlation.py      # Korrelation von eingehenden Antworten
    ├── gateway.py          # MessagingGateway-Singleton
    └── internal_adapter.py # In-Process-Adapter (Plattform-interne Benachrichtigungen)
```

### Verwendung aus Plugins

```python
# Ausgehende Nachricht senden:
await ctx.emit('messaging:outbound', {
    "provider": "discord",
    "message": "Deployment gestartet!",
    "severity": "info",
    "action_buttons": [
        {"label": "Abbrechen", "action_id": "cancel_deploy", "style": "danger"}
    ],
    "correlation_id": "deploy-42",
})

# Eingehende Antwort empfangen:
@ctx.subscribe('messaging:inbound')
async def on_reply(payload):
    if payload.get("action_id") == "cancel_deploy":
        await my_service.cancel()
```

### Provider-Adapter

Eigene Provider implementieren `GatewayAdapter` aus `core.components.messaging.logic.adapter` und registrieren sich beim `MessagingGateway`. Der Notification-Router (`notification_router`) entscheidet auf Basis der Benutzereinstellungen, welcher Provider welche Benachrichtigung erhält.

---

## Internationalisierung

Lyndrix trennt zwei Übersetzungs-Dialekte strikt durch Namespaces:

### Dialekte

| Namespace-Gruppe | Platzhalter | Konsument |
|---|---|---|
| Core/NiceGUI (`core`, `auth`, `plugins`, …) | `%{name}` | `core.i18n.t()` intern |
| React/i18next (`ui`, `settings`, Plugin-NS) | `{{name}}` + `_one`/`_other` | i18next-Client im Browser |

Die Allowlist der an Clients ausgelieferten Namespaces ist `I18NEXT_NAMESPACES = {"ui", "settings"}` in `app/core/i18n.py`. Plugins erweitern diese Allowlist durch das `i18n_namespace`-Feld im Manifest.

### HTTP-Katalog-API

Zwei unauthentifizierte Endpunkte, erreichbar bereits während des Boots (Login-Seite muss lokalisiert werden):

```
GET /api/i18n/locales
→ { default, supported[], client_namespaces[], version }

GET /api/i18n/{locale}?ns=ui,settings&v=<version>
→ 200: { locale, version, resources: { ns: { key: value } } }
→ 304: wenn v == aktuelle Version (Caching)
→ 404: unbekannte Locale
```

Der Katalog-Version ist ein Content-Fingerprint über alle registrierten Locale-Dateien. Er aktualisiert sich automatisch wenn ein Plugin seine `locales/`-Verzeichnis registriert.

### i18n-Keys prüfen

```bash
python scripts/check_i18n_keys.py
```

Englisch (`en`) ist die Quell-Locale. Nicht-englische Locales werden beim Ausliefern tief über den englischen Baum gemergt, sodass fehlende Übersetzungen automatisch auf Englisch zurückfallen.

---

## Theming

Das Theming-System besteht aus zwei Schichten:

### Schicht 1 — Token-Layer

`assets/themes/<id>/tokens.json` definiert semantische Farbtokens mit `light`- und `dark`-Varianten:

```json
{
  "light": {
    "primary": "#00d4ff",
    "bg_body": "#f8fafc",
    "text_muted": "#64748b"
  },
  "dark": {
    "primary": "#00d4ff",
    "bg_body": "#0a0f1e",
    "text_muted": "#94a3b8"
  }
}
```

### Schicht 2 — Component-Layer

`assets/themes/<id>/components.json` mappt jeden `UIStyles`-Klassennamen auf einen Tailwind-String:

```json
{
  "CARD_BASE": "rounded-xl border border-zinc-700 bg-zinc-900 p-4",
  "TITLE_H2": "text-xl font-bold text-white",
  "TEXT_MUTED": "text-zinc-400 text-sm"
}
```

Beim Start überschreibt `_hydrate_ui_styles()` die `UIStyles`-Klassenattribute mit den Werten aus `components.json`, sodass alle nachgelagerten Komponenten automatisch das aktive Theme nutzen.

### Verwendung in Plugins

```python
from core.api import UIStyles

ui.card().classes(UIStyles.CARD_BASE)
ui.label("Titel").classes(UIStyles.TITLE_H2)
ui.label("Hinweis").classes(UIStyles.TEXT_MUTED)

# Plugin-spezifische Overrides (nur für dieses Plugin, nicht global):
def setup(ctx):
    ctx.register_theme_overrides({
        "CARD_BASE": "p-6 rounded-2xl border border-violet-700 bg-violet-950",
    })
```

### CSS-Variablen (für Inline-Styles / React)

| Variable | Verwendung |
|---|---|
| `--lx-bg` | Seiten-Hintergrund |
| `--lx-surface` | Karten-/Panel-Hintergrund |
| `--lx-elevated` | Modal-/Dropdown-Hintergrund |
| `--lx-accent` | Primäre Akzentfarbe (Cyan) |
| `--lx-accent-2` | Sekundäre Akzentfarbe (Sky-Blau) |
| `--lx-accent-3` | Tertiäre Akzentfarbe (Violett) |
| `--lx-text` | Primärer Text |
| `--lx-text-muted` | Sekundärer Text |
| `--lx-border` | Akzent-Rahmen |
| `--lx-border-soft` | Subtiler Rahmen |
| `--lx-radius-sm/md/lg` | Border-Radius-Tokens |
| `--lx-glow` | Box-Shadow-Glow |
| `--lx-state-up/down/paused/unknown` | Monitor-Zustandsfarben |

### Eigenes Theme erstellen

1. `assets/themes/<id>/tokens.json` — Kopie von `default/tokens.json`, Farben anpassen.
2. `assets/themes/<id>/components.json` — Kopie von `default/components.json`, Tailwind-Klassen anpassen.
3. Fehlende Keys werden durch `UIStyles`-Standardwerte ersetzt — partielle Themes sind gültig.
4. Theme in **Einstellungen → Erscheinungsbild** auswählen oder `DEFAULT_THEME_ID=<id>` setzen.

Alternativ: ZIP mit beiden JSON-Dateien über **Einstellungen → Erscheinungsbild → Theme hochladen** einreichen.

---

## REST-API-Übersicht

Alle API-Endpunkte beginnen mit `/api/`. Authentifizierung via `Authorization: Bearer <token>` oder `X-API-Key: <key>`.

| Präfix | Authentifizierung | Beschreibung |
|---|---|---|
| `GET /api/i18n/locales` | Nein | Verfügbare Locales und Katalog-Metadaten |
| `GET /api/i18n/{locale}` | Nein | Übersetzungskatalog (i18next-kompatibel) |
| `POST /api/auth/login` | Nein | Login, liefert Access- und Refresh-Token |
| `POST /api/auth/refresh` | Nein | Token erneuern |
| `POST /api/auth/logout` | Ja | Session invalidieren |
| `GET/POST/DELETE /api/auth/keys` | Ja | API-Keys verwalten |
| `GET /api/vault/status` | Nein | Vault-Status (init/sealed/unsealed) |
| `POST /api/vault/init` | Nein | Vault initialisieren |
| `POST /api/vault/unseal` | Nein | Vault entsperren |
| `GET /api/users` | Ja | Benutzerliste |
| `GET/PUT/DELETE /api/users/{id}` | Ja | Benutzer lesen/bearbeiten/löschen |
| `GET /api/groups` | Ja | Gruppen |
| `GET /api/plugins` | Ja | Installierte Plugins auflisten |
| `POST /api/plugins/install` | Ja | Plugin installieren |
| `POST /api/plugins/{id}/enable` | Ja | Plugin aktivieren |
| `GET/PUT /api/plugins/{id}/settings` | Ja | Plugin-Settings lesen/schreiben |
| `GET /api/themes` | Ja | Themes auflisten |
| `GET /api/themes/{id}/css-vars` | Ja | CSS-Variablen des Themes |
| `GET /api/system/config` | Ja | Runtime-Config-Snapshot |
| `GET /api/system/health` | Nein | Health-Check |
| `GET /api/events` | Ja | SSE-Ereignis-Stream |
| `GET/PUT /api/me/profile` | Ja | Eigenes Profil lesen/bearbeiten |
| `POST/GET/DELETE /api/me/background` | Ja | Persönliches Hintergrundbild |
| `GET /api/users/{username}/background/{mode}` | Nein | Hintergrundbild abrufen (öffentlich) |

Plugin-Endpunkte werden unter `/api/plugins/<plugin-id>/` gemountet und durch `require_api_auth` geschützt. Plugins können eigene öffentliche Router direkt an die FastAPI-App anhängen (z.B. für Webhooks und SSE-Streams).

---

## Entwicklung

### Lokaler Dev-Stack

```bash
# Stack starten (App + DB + Vault + Docs-Preview)
docker compose -f docker/docker-compose.dev.yml up -d --build

# Endpunkte:
#   App:          http://localhost:8081
#   Vault UI:     http://localhost:8200
#   Docs-Preview: http://localhost:8000
```

Der Dev-Compose mountet `../app` direkt ins Container-Dateisystem. Codeänderungen werden durch uvicorn `--reload` sofort übernommen. Plugin-Repos werden als Geschwister-Volumes eingebunden (siehe `docker/docker-compose.dev.yml`).

### Qualitätssicherung

```bash
# Linter
ruff check app/

# Formatter
black app/

# Typprüfung
mypy app/

# Tests
pytest

# i18n-Key-Parität prüfen (en ist Quell-Locale)
python scripts/check_i18n_keys.py
```

### Versionsschema

Lyndrix Core folgt [Semantic Versioning](https://semver.org). Die aktuelle Version steht in `app/version.py`:

```python
__version__ = "0.4.0"
__release_date__ = "2026-06-28"
__codename__ = "First Light Alpha"
```

Releases sind Git-Tag-getrieben. Das Helper-Skript `../release_tag.py` taggt und pusht mehrere Repos gleichzeitig:

```bash
# Vom lyndrix-dev-Workspace aus:
./release_tag.py --version 0.4.0
```

### Plugin-Entwicklung lokal testen

```bash
# Im Plugin-Repo-Verzeichnis:
pip install -r requirements-dev.txt
pytest
mypy .
ruff check .
black --check .
```

Model- und Logic-Schichten sind ohne laufenden Core testbar. `ModuleContext` kann für Lifecycle-Tests gemockt werden (die Schnittstelle ist stabil).
