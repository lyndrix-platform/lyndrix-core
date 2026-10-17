# Analyse: Server Manager (Sweep zu Plan Abschnitt 7.4 / 10)

Repo: `plugins/lyndrix-plugin-server-manager` (Pfade unten relativ dazu). Reiner Lesezugriff, Stand 2026-10-17.

## 1. Überblick

- Manifest-ID `lyndrix.plugin.server_manager`, Version `1.3.0`, `min_core_version="0.0.6"`, `auto_enable_on_install=False` (`entrypoint.py:78-89`); `react_ui=True`, `i18n_namespace="server_manager"`, Routen `/server-manager` (Sidebar) und `/server-manager/settings` (`entrypoint.py:91-111`).
- `permissions.subscribe`: `db:connected`, `server_manager:register_server`, `:deregister_server`, `:writeback_failed`, `:catalog_changed`, `plugin:settings_changed` (`entrypoint.py:113-122`).
- `permissions.emit`: `server_created`, `server_updated`, `server_deleted`, `hardware_changed`, `status_changed`, `ingest_failed`, `writeback_requested`, `catalog_changed` (`entrypoint.py:123-132`).
- Rollen `admin`/`operator` (api:read+write), `viewer` (api:read), jeweils als `plugin:lyndrix.plugin.server_manager:api:*` (`entrypoint.py:137-152`).
- **Kein `settings_schema`, kein `placement`, kein `panels`, kein `ai_instructions`** im Manifest. Einstellungen = NiceGUI-`render_settings_ui` (`entrypoint.py:157`, `app/ui/settings.py:15`) und React `SettingsView` (`src/ui/PluginApp.tsx:380`); einziges Setting ist der Katalogpfad, persistiert per `ctx.set_secret("catalog_dir")` (`app/controller/service.py:74,153`).
- Das Plugin **abonniert `iac:inventory_updated` nicht** (grep ohne Treffer im Repo). Hosts kommen ausschließlich über `server_manager:register_server`/REST (Abschnitt 3).
- Layout weicht vom Kanon ab: `app/controller/` statt `app/logic/`, `src/ui/` statt `app/ui/react/` (Verzeichnisliste; `CLAUDE.md` nennt die Umbenennung controller→logic, hier noch nicht erfolgt). Bundle liegt in `ui_static/ui_bundle.js` (Legacy-Pfad).

## 2. Datenmodell

Eine Tabelle `server_manager_servers` (`app/model/models.py:12-13`, eigene `_Base`, `models.py:8`):

| Spalte | Typ | Beleg |
|---|---|---|
| `id` | Integer PK | `models.py:21` |
| `name` (NN), `hostname` (idx) | String(255) | `models.py:22-23` |
| `environment_id` (NN) | String(64), Format `site:stage` | `models.py:24`; Orchestrator-Wert `engine.py:1262` |
| `server_type` (NN) | `physical|vm|container` | `models.py:25` |
| `product_id`, `service_class_id`, `os_family_id`, `os_version_id`, `os_type` | String | `models.py:26-30` |
| `status` | String(64), default `active` | `models.py:32` |
| `hardware_profile` (JSON), `tags` (JSON), `notes` (Text) | | `models.py:33-35` |
| `source`, `external_id` | String(64)/(255), NULL = manuell | `models.py:39-40` |
| **`ci_id`** | String(36), Index `ix_server_manager_servers_ci_id` | `models.py:43`, `database.py:51` |
| `writeback` (Bool), `sync_state` (`pending|failed|NULL`), `sync_error` | | `models.py:47,51-52` |
| `ordered_by`, `ordered_at`, `created_at`, `updated_at` | | `models.py:57-60` |

- Unique `uq_server_manager_source_ext (source, external_id)` (`models.py:17-19`); NULLs kollidieren nicht (manuelle Records).
- `read_only` = `source is not None and not writeback` (`models.py:54-56`); `to_dict()` liefert alle Felder inkl. `ci_id`, `read_only` (`models.py:62-89`).
- **`ci_id` wird nicht vom Plugin aufgelöst.** Kein `entity_registry:identify`/`lookup` im Code (grep leer). Er kommt vom pushenden Source (Orchestrator setzt `"ci_id": host.get("ci_id")`, `plugins/lyndrix-plugin-iac-orchestrator/app/controller/engine.py:1264`; der Orchestrator holt ihn vorher via `_attach_ci_ids`, `engine.py:1285`) oder von Hand (`ServerCreatePayload.ci_id`, `app/api.py:54`; Länge exakt 36, `api.py:54,74`). `ci_id` ist in `_UPDATABLE_FIELDS` (`service.py:60-66`), wird also von jedem Push überschrieben, wenn er mitgeschickt wird und abweicht.
- Migration: additive `ALTER TABLE ... ADD COLUMN` aus `_NEW_COLUMNS` (`database.py:34-46`), Indizes `_INDEXES` (`database.py:49-52`), unter `db_instance.advisory_lock("lyndrix_bootstrap")` (`database.py:71-96`), Race-Codes 1050/1060/1061 toleriert (`database.py:22-23`).
- **Relationen: keine.** Weder Services/Container pro Host noch Parent/Child (Hypervisor→VM). `tags` sind nur Ansible-Gruppennamen (Orchestrator filtert `site_`/`stage_`, max. 25, `engine.py:1266-1268`). `hardware_profile` ist ein freies JSON je `server_type`. Es gibt keine Spalte für VM-ID oder Hypervisor (der Orchestrator sendet `terraform.vm_id` nur zur Ableitung von `server_type`, `engine.py:1247-1253`, und gibt es nicht weiter).

## 3. Ingest-Pfade und Events

**Ingest (alle münden in `ServerManagerService.upsert_external_server`, `service.py:382-418`):**

| Pfad | Stelle | Verhalten |
|---|---|---|
| Bus `server_manager:register_server` | `entrypoint.py:281-296` | Payload nicht-dict → `ingest_failed`; `wait_ready()` (max 30 s, `service.py:122-128`); Validierung via `ExternalServerPayload` (`api.py:88-97`), `asyncio.to_thread(upsert)` |
| Bus `server_manager:deregister_server` | `entrypoint.py:298-317` | `{source, external_id}` → `remove_external_server` (`service.py:420-424`) |
| Bus `server_manager:writeback_failed` | `entrypoint.py:319-331` | `mark_writeback_failed` (`service.py:426-451`) setzt `sync_state="failed"` |
| REST `PUT /servers/external/{source}/{id}` | `api.py:371-380` | `ExternalServerBody` (`api.py:77-85`), 400 bei ValueError, 409 bei LookupError |
| REST `POST /servers/import` (Batch, max 1000) | `api.py:395-417` | je Item eigenes Ergebnis |

- Upsert: Schlüssel `(source, external_id)` über `_find_external_id` (`service.py:373-380`); neu → Pflichtfelder `name, environment_id, server_type` (`service.py:69,401-403`), `IntegrityError` bei parallelem Insert → als Update (`service.py:404-411`); sonst PATCH-Semantik nur auf `_UPDATABLE_FIELDS` (`service.py:397,415`). Ein Push setzt `sync_state/sync_error` zurück und übernimmt `writeback` (`service.py:291-297`). Manuelle Records (source NULL) werden nie getroffen (Test `tests/test_ingest.py:79`).
- Orchestrator-Payload (einziger bekannter Produzent): `source="iac-orchestrator"`, `external_id=host["name"]`, `name`, `hostname`, `environment_id`, `server_type`, `status="active"`, `writeback=False`, `ci_id`, `tags`, `notes` (`engine.py:1255-1269`). Ausgelöst in `emit_iac_inventory_updated` (`engine.py:1274-1294`), parallel zu `iac:inventory_updated {hosts}` (`engine.py:1286`), das der Server Manager ignoriert.
- Es gibt **kein Löschen verwaister Hosts** durch Full-Sync; `deregister_server` muss explizit kommen (kein `replaces`-Semantik im Plugin).

**Emittierte Events (`ServerManagerService._emit`, `service.py:518-534`, thread-sicher via `call_soon_threadsafe`):**

| Topic | Payload | Stelle |
|---|---|---|
| `server_manager:server_created` | `to_dict()` | `service.py:247` |
| `server_manager:server_updated` | `to_dict() + changes{field:{old,new}}` (nur `_UPDATABLE_FIELDS`, daher inkl. `ci_id`) | `service.py:307-313` |
| `server_manager:server_deleted` | letzter `to_dict()` | `service.py:364` |
| `server_manager:hardware_changed` | `{server_id, server_name, action, old_profile, new_profile, environment_id, server_type}` | `service.py:250,316` |
| `server_manager:status_changed` | `{server_id, server_name, old_status, new_status}` | `service.py:327` |
| `server_manager:ingest_failed` | `{topic, source, external_id, error}` | `entrypoint.py:271-279` |
| `server_manager:writeback_requested` | `{action, source, external_id, server, changes}`; **nur lokaler Bus** (kein `cluster=True`) | `service.py:507-516` |
| `server_manager:catalog_changed` | `{catalog_dir, action}`, `cluster=True` | `service.py:181-186` |

Konsumiert zusätzlich: `plugin:settings_changed` (lädt Katalogpfad neu, `entrypoint.py:263-266`), `db:connected` (Bootstrap, `entrypoint.py:251-254`).

## 4. API-Routen

Mount `/api/plugins/lyndrix.plugin.server_manager/` (`entrypoint.py:237`); Permissions `plugin:<id>:api:read|write` (`api.py:34-36`). Alle Handler nutzen `asyncio.to_thread`; DB-Routen geben 503 bis `is_ready` (`api.py:290-296`).

| Methode | Pfad | Perm | Stelle |
|---|---|---|---|
| GET | `/servers` | read | `api.py:299` |
| GET | `/servers/{server_id}` | read | `api.py:304` |
| POST | `/servers` | write | `api.py:315` |
| PATCH | `/servers/{server_id}` (409 bei read-only) | write | `api.py:329` |
| DELETE | `/servers/{server_id}` | write | `api.py:345` |
| PUT | `/servers/external/{source}/{external_id}` | write | `api.py:371` |
| DELETE | `/servers/external/{source}/{external_id}` | write | `api.py:382` |
| POST | `/servers/import` | write | `api.py:395` |
| GET | `/statuses` | read | `api.py:420` |
| GET | `/stats` | read | `api.py:424` |
| POST | `/validate` | read | `api.py:430` |
| POST | `/catalog/evaluate-combo` | read | `api.py:447` |
| GET | `/catalog` | read | `api.py:469` |
| POST | `/catalog/reload` | write | `api.py:473` |
| POST | `/catalog/path` | write | `api.py:481` |

Es gibt keine `/health`-, `/resolve`- oder `/ai/instructions`-Route; `/settings` wird (korrekt) nicht verwendet. Es gibt keinen Filter `?ci_id=` auf `/servers`.

## 5. UI-Komponenten

React (`src/ui/`): `index.tsx` (10 Z.), `PluginApp.tsx` (533 Z.), `ServerWizard.tsx` (1084 Z.), `lib/api.ts` (340 Z.).

- `PluginApp` (`PluginApp.tsx:529`): Router nur über `pathname.endsWith('/settings')`; kein `panel`-Prop, keine Drawer-/Detailansicht.
- `ServerManagerView` (`:504`): Zustand `list | form`; Klick auf eine Zeile öffnet **den Wizard** (`ServerWizard`, `:158`), nur für nicht-read-only Records (`:297-299`). Read-only (IaC-)Hosts haben **keine Detailansicht**.
- `ServerListView` (`:162`): lädt `serverManagerApi.listServers()` (`:183`), Filter Umgebung/Status/Typ (`:249-262`, `useMemo` `:196`), Tabelle `lx-table` Spalten Name / Connection / Status / Aktionen (`:284-293`).
- Zeilenzellen: Connection-Zelle (`:303-337`) enthält `hostname · environment · type` plus die ci_id-Icons "Registry öffnen" (`:306-322`) und "Incident melden" (`:323-336`); Status-Zelle (`:338-342`) = `<StatusBadge>` (`:90`) + `<SyncIndicator>` (`:106`); Aktionszelle (`:344+`, Schloss für read-only).
- Typ `ServerRecord` in `src/ui/lib/api.ts:6-35` (inkl. `ci_id`); API-Wrapper `serverManagerApi` (`api.ts:~300-316`).
- Styles nutzen `lx-badge`, `lx-dot`, `--lx-state-up/down/paused/unknown` (`PluginApp.tsx:81-103`).
- i18n: `locales/server_manager.{de,en}.json`, Hook `useTranslation('server_manager')` (`PluginApp.tsx:107`).

NiceGUI (noch vorhanden und aktiv): `@ui.page("/server-manager")` (`entrypoint.py:333-336`), `app/ui/page.py` (Kopf, Stats, Liste), `app/ui/overview.py` (Liste, Status-Farben `:192`, Umgebungs-Badge `:220`), `app/ui/widget.py` (Dashboard-Widget), `app/ui/settings.py`, 3-Schritt-Konfigurator `app/controller/configurator/step{1,2,3}.py`. Die NiceGUI-Seite importiert `ui.layout.main_layout` aus Core-Internals mit Fallback (`entrypoint.py:57-67`).

## 6. Teststand (Referenz-Rezept)

- 3 Dateien, 36 Tests: `tests/test_smoke.py` (Katalog-Parser, Regeln; 8 Tests), `tests/test_ingest.py` (Upsert, Idempotenz, Writeback, Thread-Emit; 14), `tests/test_cluster.py` (DDL unter Advisory-Lock, Race-Toleranz, 1020-Retry, Katalogpfad; 12).
- `tests/conftest.py:9` importiert `core.services` vor allem anderen (Zirkelimport `core.api` ↔ `ui.layout`, TODO im File). Folge: Tests brauchen den Core im `PYTHONPATH`; `app.*` wird aus dem Repo-Root importiert (nicht über `entrypoint`).
- Fixture-Muster (`tests/test_ingest.py:19-42`): `_FakeCtx` mit `events`-Liste und `emit(topic, payload)` (in `test_cluster.py:21` mit `cluster=False`-Parameter, plus `set_secret/get_secret`), In-Memory-SQLite `create_engine("sqlite://", poolclass=StaticPool, check_same_thread=False)`, `fake_db = SimpleNamespace(engine=, SessionLocal=sessionmaker(...), is_connected=True)` per `monkeypatch.setattr(database, "db_instance", fake_db)`; dann `ServerManagerService()`, `set_context(_FakeCtx())`, `on_db_connected()`. `test_cluster.py:37` ergänzt `advisory_lock=lock` am Fake.
- Aufruf: `pip install -r requirements-dev.txt` (pytest, pytest-asyncio, pytest-cov, mypy/ruff/black; `requirements-dev.txt:1-3`) und `pytest` im Repo-Root (`documentation.md:148-167`). Laut Workspace-Regel (CLAUDE.md "DB gotchas") im Container `lyndrix-core-dev` per `docker cp` + `PYTHONPATH=/app`, bei Cluster-Bench zusätzlich `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local`. Nicht ausgeführt (read-only Sweep).
- Nicht getestet: Entrypoint-/`setup(ctx)`-Handler, API-Router (kein `TestClient`), React-UI. Neue Handler (Abschnitt 8) sollten als reine Service-Methoden gebaut werden, damit das vorhandene Fixture reicht.
- Dev-Run: `.claude/skills/run-server-manager/SKILL.md` (Plugin ist in `docker-compose.dev.yml` nach `/app/plugins/server_manager` gemountet).

## 7. Cluster-Besonderheiten

- **Keine Leases, kein `placement`, kein `ctx.on_lease`, kein `ctx.my_shard`, kein `ctx.set_status`** (grep leer). Alle Instanzen verarbeiten alle Bus-Events; Einmal-Arbeit wird nicht koordiniert.
- Bus-getriebene Schreibzugriffe sind trotzdem sicher, weil sie idempotent sind: Upsert per Unique-Key mit IntegrityError-Fallback (`service.py:404-411`), `retry_write` bei 1020/1205/1213 (`database.py:149-163`), READ-COMMITTED-Session (`database.py:166-189`), DDL unter `lyndrix_bootstrap`-Lock.
- Wichtig für Abschnitt 8: `register_server` ist **lokaler Bus** (Orchestrator `ctx.emit` ohne `cluster=True`, `engine.py:1255`); es landet nur auf der Instanz, auf der der Orchestrator-Sync läuft (`pinned:runner`). Die DB ist gemeinsam, daher sehen alle Instanzen das Ergebnis. Ein `maintenance:*`-Event müsste dagegen von den Peers (UI/API laufen auf jeder Instanz) per DB gelesen werden; in-process-Zustand reicht nicht.
- `server_manager:writeback_requested` ist bewusst nur lokal; TODO für leased Re-Emitter (`service.py:18`).
- `catalog_changed` ist der einzige `cluster=True`-Emit (`service.py:185`); Empfang nur bei `payload["_origin"]` (`entrypoint.py:259`).
- `health()` prüft DB, Tabellen, `COUNT(*)`, Katalog (`entrypoint.py:167-216`), meldet Standby korrekt (kein Lease-Konzept).

## 8. Einhängepunkte für Plan 7.4

**Ausgangslage:** Der Server Manager kennt pro Host nur `ci_id` (nullable) und `(source, external_id)`. Hosts aus dem Orchestrator tragen `ci_id` bereits (`engine.py:1264`), manuelle nur, wenn der Operator ihn setzt.

### 8.1 Read-Model (neue Tabelle empfohlen)
- **Neue Tabelle** `server_manager_maintenance` (Modell in `app/model/models.py`, gleiche `_Base`). Vorschlag: `ci_id` (String(36), idx), `kind` (`window|override`), `ref` (`override_id` oder Fenster-Index), `source` (String(64)), `schedule` (JSON, Schedule-Dicts aus Plan §5), `inherited_from` (String(36) NULL), `until` (DateTime NULL, nur Override), `reason` (Text), `updated_at`. Eindeutig `(kind, ci_id, ref)` bzw. für Fenster `(source, ci_id)` mit JSON-Liste.
  Begründung gegen JSON-Spalte an `server_manager_servers`: Events kommen pro `ci_id`, auch für CIs, die (noch) keinen Server-Record haben (kind `service`, Reihenfolge `windows_published` vor `register_server`); `replaces: true` pro `source` (Plan §6) lässt sich als `DELETE WHERE source=? AND kind='window'` + Insert in einer Transaktion sauber umsetzen. Eine JSON-Spalte wäre nur bei `ci_id`-Match schreibbar und würde bei jedem `register_server`-Push (PATCH nur auf `_UPDATABLE_FIELDS`, `service.py:397`) zwar nicht überschrieben, aber bei einem späteren ci_id-Zuordnen nie nachgezogen.
- `app/model/database.py`: neue Tabelle entsteht automatisch über `create_all` (`database.py:89`) und fällt unter den Advisory-Lock; kein `_NEW_COLUMNS`-Eintrag nötig. Risiko: Die Migrationslogik prüft nur `_TABLE` (`database.py:120,136`); für die neue Tabelle genügt `create_all`.
- `app/controller/service.py`: neue Methoden `apply_windows_published(payload)`, `apply_override_started(payload)`, `apply_override_ended(payload)` (blockierend, `retry_write`, kurze READ-COMMITTED-Transaktionen, Chunks wegen `max_statement_time`), plus Lesemethode `maintenance_for(ci_ids) -> dict[ci_id, {windows, active_override, effective_until}]`. Effektives Fenster/`active_until`/`next_start` aus dem geplanten `core.api.maintenance` (API 1.13, Plan §5) berechnen; **nicht** in der DB speichern, sondern beim Lesen (`now` abhängig). Fallback bei älterem Core: `getattr`-Guard auf den Import, sonst nur Rohdaten anzeigen.
- `entrypoint.py`: drei `@ctx.subscribe(...)`-Handler analog `_on_register_server` (`entrypoint.py:281-296`): `isinstance(payload, dict)`-Check, `await svc.wait_ready()`, `asyncio.to_thread(...)`, Fehler nur loggen (die Topics gehören nicht in `ingest_failed`, dessen Payload ist `source/external_id`-förmig; ggf. eigener Log). Unbekannte Felder ignorieren (Plan §6).
- `entrypoint.py` Manifest: `permissions.subscribe` um `maintenance:windows_published`, `maintenance:override_started`, `maintenance:override_ended` erweitern (`entrypoint.py:113-122`). Version auf nächste Minor (1.4.0) heben, `min_core_version` nicht erhöhen, solange der Code `getattr`-guarded bleibt.
- **Cluster-Risiko:** Die Maintenance-Events müssen vom Emitter mit `cluster=True` gesendet werden, sonst erreichen sie nur eine Instanz; da der Zustand in der gemeinsamen DB liegt, sind Doppel-Verarbeitung (jede Instanz schreibt dieselbe Zeile) und verlorene Events (Instanz nicht erreicht) die zwei Fälle. Doppelt: idempotent lösbar (delete+insert pro `source`, Upsert per Override-ID). Verloren: akzeptabel, weil Orchestrator beim nächsten Sync `replaces: true` neu sendet; Overrides haben `until` und verfallen ohnehin. Peer-Events tragen `payload["_origin"]` (Muster `entrypoint.py:259`) – hier nicht filtern, sonst gehen Events der eigenen Instanz verloren; stattdessen idempotent schreiben.
- Aufräumen: abgelaufene Overrides (`until < now`) beim Lesen ignorieren und gelegentlich beim Schreiben löschen; kein Lease nötig.

### 8.2 Anzeige am Host
- `app/api.py`: Variante A (minimal, rückwärtskompatibel): `to_dict()`-Ergebnis in `list_servers`/`get_server` (`api.py:299-313`) um ein Feld `maintenance: {windows[], active_override, in_maintenance, next_start, active_until}` anreichern, per **einer** Batch-Abfrage über alle `ci_id` (kein N+1; Tabellen klein). Variante B: separate Route `GET /servers/{id}/maintenance` und `GET /maintenance?ci_id=` (read-Perm). Empfehlung A plus B für Detail; `ServerRecord.to_dict()` (`models.py:62`) bleibt unberührt, da die Anreicherung im Service passiert.
- `src/ui/lib/api.ts:6-35`: `ServerRecord` um optionales `maintenance?: ...` erweitern.
- `src/ui/PluginApp.tsx`: neue Komponente `MaintenanceBadge` neben `SyncIndicator` (`:106`) und Einbindung in der Status-Zelle (`:338-342`); Tooltip mit Quelle, `active_until`/`next_start`. Zusätzlich in der Connection-Zelle (`:303-337`) keine Änderung nötig. Ein Filter "in Wartung" kann bei den Selects (`:249-262`) ergänzt werden (Logik im `useMemo` `:196`).
- **Detailansicht fehlt:** Es gibt keinen Drawer; für read-only-Hosts reagiert die Zeile nicht auf Klick (`:297-299`). Für "effektives Fenster + laufende Overrides" braucht es eine neue Detail-/Expand-Ansicht (neue Komponente `ServerMaintenancePanel`, in `ServerManagerView` `:504` als dritter `view.kind`, oder aufklappbare Zeile). Das ist der größte UI-Aufwand; Wizard (`ServerWizard.tsx`) ist dafür ungeeignet (Bearbeitungsformular, nicht für read-only Hosts erreichbar).
- i18n: neue Schlüssel in `locales/server_manager.de.json` und `.en.json` (`i18next`-Dialekt `{{name}}`).
- NiceGUI-Seite (`app/ui/overview.py:192-220`): bleibt zunächst ohne Anzeige (React ist Migrationsziel); in der Abwägung ggf. ein Badge nachziehen.

### 8.3 Services pro Host
**Nicht möglich ohne Erweiterung.** Weder Relation noch Liste im Plugin (Abschnitt 2). Optionen: (a) Server Manager fragt bei Bedarf Entity Registry (`entity_registry:lookup`, CLAUDE.md "Configuration-item ids") bzw. holt Services über Docker-Manager/Orchestrator – das wäre ein neuer optionaler `ctx.request`-Aufruf (Antwort `None` → degradieren); (b) die betroffenen Services werden vom Emitter im `windows_published`-Event mitgeliefert (`inherited_from`, Plan §6, deckt Vererbung Host→Service, aber nicht die Gegenrichtung); (c) UI verlinkt nur auf Registry/Monitoring. Empfehlung: im ersten Wurf (c), "betroffene Services" über Registry-Deep-Link (Muster `PluginApp.tsx:306-322`).

### 8.4 Tests
Neue Datei `tests/test_maintenance.py` mit dem Fixture aus `tests/test_ingest.py:19-42` (SQLite, `_FakeCtx`); Fälle: `replaces` pro Quelle, Idempotenz, Override-Ende, Ablauf, Event für CI ohne Server-Record, SQLite mit `PRAGMA foreign_keys=ON`.

## 9. Abweichungen vom Plan / offene Fragen

1. Plan §6 nennt den Server Manager als Konsumenten von `maintenance:windows_published` und Plan §4.2/§7.4 erwartet `ci_id` je Host. Real: `ci_id` ist nullable und nur gesetzt, wenn die Quelle ihn schickt; manuelle Hosts haben i. d. R. keinen. Events ohne passenden Host-Record müssen entweder gespeichert (empfohlen, eigene Tabelle) oder verworfen werden – Entscheidung nötig.
2. Plan §10-Briefing erwartet "`iac:inventory_updated`-Nutzung" im Server Manager. Real: keine; der Orchestrator pusht stattdessen `server_manager:register_server` (`engine.py:1255`). Für Wartungsfenster ist das unkritisch, da §6 eigene Events nutzt.
3. Plan §10 erwartet Detail-/Drawer-Anzeigeorte. Real: nur Tabellenzeile; Detailansicht existiert nicht (Abschnitt 5). Soll sie neu entstehen oder reicht das Badge?
4. Kein Weg, "betroffene Services" zu listen (Abschnitt 8.3).
5. `register_server`/`writeback_requested` sind lokal-Bus; für `maintenance:*` muss der Emitter `cluster=True` setzen – im Plan §6 nicht festgelegt, bitte ergänzen.
6. Verzeichnisstruktur nicht kanonisch (`controller/`, `src/ui/`); Neubau sollte die vorhandene Struktur beibehalten (Umbenennung getrennt, nicht in diesem Feature).
7. Kein Core-Zeitzonen-/Cron-Helfer vorhanden, bis `core.api.maintenance` (API 1.13) existiert; `min_core_version` bleibt 0.0.6 nur, wenn die Anzeige ohne die Bibliothek degradiert.
8. Offen: Soll der Server Manager manuelle Overrides auslösen können (Plan §7.4 sagt für Docker-Manager/External Services "später")? Aktuell nur Anzeige; kein Emit-Recht nötig (`permissions.emit` bleibt unverändert).
