# Analyse ITSM-Plugin für Wartungsfenster (Plan Abschnitt 7.3)

Read-only Sweep, Stand `lyndrix-plugin-itsm` 0.2.0. Pfade relativ zu
`/home/marvin/gitlab/lyndrix-dev/plugins/lyndrix-plugin-itsm`. Zeilenangaben `Datei:Zeile`.

## 1. Überblick

- Manifest-ID `lyndrix.plugin.itsm` (`entrypoint.py:69`), Version `0.2.0` (`entrypoint.py:76`),
  `min_core_version="1.4.0"` (`entrypoint.py:85`), `auto_enable_on_install=True` (`:86`).
  Kein `placement` gesetzt (Default `any`), keine Lease-Nutzung (siehe Abschnitt 8).
- Permissions subscribe (`entrypoint.py:110-125`): `db:connected`, `vault:ready_for_data`,
  `plugin:settings_changed`, `itsm:catalog_reloaded`, `itsm:record_create|record_update|transition|journal_add|ci_link`,
  `entity_registry:lookup_result`, eigene Broadcasts `itsm:record_updated|state_changed|journal_added`.
  Emit (`:127-140`): `itsm:record_created|record_updated|state_changed|journal_added`, `itsm:*_result`,
  `itsm:ingest_failed`, `itsm:catalog_reloaded`, `entity_registry:register`, `entity_registry:lookup`.
  **`monitoring:outage_persisted` / `monitoring:outage_recovered` fehlen in subscribe.**
- REST-Permissions: `plugin:lyndrix.plugin.itsm:api:read|api:write|itsm:admin` (`app/api/__init__.py:22-25`);
  Rollen admin/agent/viewer (`entrypoint.py:155-169`).
- Schichten: `entrypoint.py` (Wiring, 397 Zeilen), `app/logic/service.py` (1063 Z.), `app/logic/catalog.py`,
  `app/api/__init__.py`, `app/model/{models,database}.py`, React `app/ui/react/PluginApp.tsx` (993 Z.).
  Kein `app/ui/nicegui/` (nur React).
- Settings (`entrypoint.py:98-108`): `attachment_dir`, `max_attachment_mb`, `catalog_dir`. Keine ITSM-Regel-/Monitoring-Settings.

## 2. Datenmodell

Alle Tabellen in `app/model/models.py`, SQLAlchemy `_Base` (eigene Declarative Base, `:34`).

**`itsm_records`** (`Record`, `models.py:42-120`) - eine Tabelle für alle Typen:
`uuid` PK (`:50`), `number` unique (`:51`, z. B. `INC0000042`), `record_type` (`:52`), `subtype`, `category` (`:53-54`),
`title` (255), `description` (`:56-57`), `state` (`:59`), `impact`/`urgency` Int (`:60-61`), `priority` String(8)
abgeleitet aus Matrix (`:62`), `assignment_group`, `assignee`, `requester`, `created_by` (`:64-67`),
`custom_fields` JSON, `tags` JSON (`:69-70`), `opened_at`, `resolved_at`, `closed_at`, `due_at`, `version`
(`:72-76`), Gateway-Felder `source`, `external_id`, `external_url`, `sync_state`, `sync_error` (`:80-84`),
`created_at`, `updated_at`. **Unique `(source, external_id)`** = `uq_itsm_source_ext` (`:44-48`).
Es gibt **kein** Feld `severity` und **keine** Spalte für Regel-/Monitor-IDs; solche Daten gehören in
`custom_fields` oder `tags` (JSON, nicht indiziert).

**`itsm_journal`** (`JournalEntry`, `models.py:123-145`): `record_uuid` FK, `kind`
(`comment | work_note | state_change | field_change | link | attachment | system`, Kommentar `:128-129`),
`author`, `body`, `data` JSON, `created_at`. Append-only (Audit-Trail, `models.py:9-10`).

**`itsm_ci_links`** (`CiLink`, `models.py:148-170`): `record_uuid` FK, `entity_uuid` String(36) (= Entity-Registry-UUID = `ci_id`),
`role` String(32), gecachte `display_name`, `entity_type`; Unique `(record_uuid, entity_uuid, role)` (`:150-152`).
Erlaubte Rollen: `("affected", "caused_by", "changed", "related")` (`app/logic/catalog.py:46`).
`ci_id` heißt in ITSM durchgehend `entity_uuid`; die API-Antwort hängt `ci_links` ans Record-Detail (`service.py:299-340`, `_detail`).

Weitere: `itsm_record_links` (`models.py:173-192`, Relationen in `catalog.py:33-43`), `itsm_attachments` (`:195-218`),
`itsm_counters` (`:221-225`). Nummernvergabe `ItsmService._allocate_number` (`service.py:214-246`, eigene READ-COMMITTED-Tx).
Migrationen: `database.py:34` (`_migrate_columns`), DDL unter `advisory_lock("lyndrix_bootstrap")` (`database.py:82-87`).

## 3. Katalog

`catalog/record_types/{incident,change,problem,request,task}.yml`, geladen von `CatalogLoader` (`app/logic/catalog.py`;
`service.catalog.load()` in `entrypoint.py:197`, Reload `service.py:170-175`).

**Incident** (`catalog/record_types/incident.yml`):
- `prefix: INC`, `number_width: 7` (`:8-9`); `subtypes`: outage, degradation, security, data (`:14-18`);
  Kategorien u. a. `compute` (hypervisor, vm, container), `network`, `storage`, `application`, `monitoring` (`:20-41`).
- **Kein Feld "severity"**. Priorität = Matrix aus `impact` (1-3) x `urgency` (1-3) (`:43-68`), Default `{impact:3, urgency:3}` (`:52`):
  `1-1 P1; 1-2 P2; 1-3 P3; 2-1 P2; 2-2 P3; 2-3 P4; 3-1 P3; 3-2 P4; 3-3 P4` (`:54-63`);
  Levels P1 Critical, P2 High, P3 Medium, P4 Low (`:64-68`). `derive_priority` `catalog.py:185-188`.
- **Zustände** (`:72-78`): `new` (initial, open), `in_progress` (open), `on_hold` (open, requires `hold_reason`),
  `resolved` (kind resolved, requires `resolution_code`, `resolution_notes`), `closed` (final), `cancelled` (final, requires `cancel_reason`).
- **Übergänge** (`:80-84`): `new -> in_progress|resolved|cancelled`; `in_progress -> on_hold|resolved|cancelled`;
  `on_hold -> in_progress|cancelled`; `resolved -> closed|in_progress`. Direkt `new -> resolved` ist erlaubt.
- Felder (`:88-108`): `resolution_code` select (fixed, workaround, no_fault_found, duplicate, cannot_reproduce), `resolution_notes` text,
  `hold_reason`, `cancel_reason`, `affected_users`, `security_relevant`. Assignment-Groups: service_desk, infra, network, security (`:110-114`).
- Wichtig: `resolved` setzt `resolved_at` und lässt `closed_at` NULL; erst `closed|cancelled` setzt `closed_at` (`service.py:626-634`).
  "Offen" in `list_records` (`include_closed=False`) heißt `closed_at IS NULL` (`service.py:475-476`), schließt also `resolved` **ein**.
  Ein Dedupe "offen" muss daher über den State-Kind (`open`) laufen, nicht über `closed_at`.

Change-Typ: Zustände ab `change.yml:60-68` (u. a. `assess` requires risk/implementation_plan/rollback_plan, `approval` requires
`planned_start`/`planned_end`, `closed` requires `close_code`) - relevant für die spätere Stufe (Abschnitt 9, Punkt 6).

## 4. Erzeugungs- und Übergangspfade

**Kern**: `ItsmService.create_record(data, author)` (`service.py:392-457`): validiert Klassifikation (`_validate_classification`, `:342-388`),
`impact`/`urgency` gegen Katalog, Default aus Katalog; `priority = rt.derive_priority(...)` (`:421`); initialer State (`:420`);
`requester = fields.get("requester") or author`, `created_by = author` (`:423-424`); CI-Links müssen bereits `entity_uuid` tragen (`_check_ci_links`, `:664-674`);
`retry_write` bei MariaDB 1020/1205/1213 (`:439-443`); Journal-Eintrag `system`/"created" (`:433`); danach
`itsm:record_created {record}` (`:455`) und `entity_registry:register` (`:456`, `_announce_entity` `:1017-1031`).

**REST**: `POST /records` (`api/__init__.py:155-171`), Payload `RecordCreate` (`:47-65`). `source`/`external_id` nur mit `itsm:admin`
(`:163-167`). CI-Referenzen werden vor dem Service aufgelöst (`:169`).

**Bus** (`entrypoint.py`):
- `itsm:record_create` (`:261-277`): Payload = `RecordCreate`-Felder + `author` (Default `"bus"`, `:268`) + optional `request_id`;
  **keine Permission-Prüfung auf Inhalt, `source`/`external_id` sind hier ohne Admin-Check erlaubt**; Antwort auf `itsm:record_create_result`
  (`:275`/`:277`, über `_reply` `:253-257`). Ohne `request_id` und bei Fehler -> `itsm:ingest_failed` (`:249-257`).
- `itsm:transition` (`:325-343`): `{ref, to_state, fields?, note?, author, version?, request_id?}`, Antwort `itsm:transition_result`.
- `itsm:journal_add` (`:345-359`): `{ref, kind (Default "work_note"), body, author}`; **kein Reply**, Fehler nur `itsm:ingest_failed`.
  Erlaubte Kinds nur `comment|work_note` (`service.py:30`, Prüfung `:646-647`).
- `itsm:record_update` (`:306-323`), `itsm:ci_link` (`:279-304`, `ref` + `ci|entity_uuid` + `role`).

**CI-Auflösung**: `ItsmService.resolve_ci` (`service.py:678-719`) -> `ctx.request("entity_registry:lookup", {"ref": ref},
reply_topic="entity_registry:lookup_result", timeout=5.0)` (`:695-698`). Rückgabe `None`, wenn Registry fehlt/Timeout/`ctx.request` fehlt (`:692-702`);
`ItsmError` bei unbekannt/mehrdeutig (`:703-713`); ITSM-Records werden als CI abgelehnt (`:714-717`).
`resolve_ci_links` (`:721-750`): UUID wird trotzdem über die Registry geführt (Merge-Tombstones, Namen-Cache, `:740-744`);
ohne Registry geht ein roher UUID-Link durch, ein Name allein schlägt fehl (`:745-748`). Es ist **async** - für Aufrufe aus Bus-Handlern passt das,
`create_record` selbst ist synchron und läuft per `asyncio.to_thread` (`entrypoint.py:274`).
Kein `/resolve`-REST-Aufruf aus ITSM; das UI nutzt `registryApi` (`PluginApp.tsx:408`).

**Zustandsübergang**: `transition()` (`service.py:569-580`) -> `_transition_tx` (`:582-640`): Version prüfen (`:587-590`), Ziel-State bekannt (`:593-595`),
Übergang laut Katalog (`:596-600`), `fields` anwenden (Kernfelder `assignee|assignment_group|category|subtype|description`, Rest = Custom, `:603-612`),
`requires` des Zielstates prüfen (`:614-621`), Zeitstempel (`:626-634`), Journal `state_change` mit `note` als Body (`:635`), `version+1` per
bedingtes UPDATE (`_bump_version` `:247-258`), danach `itsm:state_changed` (`:579`). Fehler: `TransitionError` (409), `ItsmError` (400), `VersionConflict` (409).

**Resolve/Close, Systemakteur**: Es gibt keine spezielle Resolve-Funktion; Resolve = `transition(ref, "resolved", fields={"resolution_code": "fixed",
"resolution_notes": "..."}, note=..., author="monitoring")`. `author` ist ein freier String (kein User-Objekt, keine Prüfung); API setzt
`author=identity.username` (z. B. `api/__init__.py:170,199`), Bus default `"bus"` (`entrypoint.py:268,336`). Ein Systemakteur (`"monitoring"`)
ist ohne Codeänderung möglich. Audit-Trail = Journal-Einträge mit `author`.
**Kommentar**: `add_journal(ref, "comment"|"work_note", body, author)` (`service.py:644-660`); Event `itsm:journal_added`, Benachrichtigung nur bei `kind == "comment"` (`entrypoint.py:392-393`).

## 5. API-Routen (Mount `/api/plugins/lyndrix.plugin.itsm/`)

| Methode + Pfad | Permission | Service | Quelle |
|---|---|---|---|
| GET `/records` (type, state, priority, assignee, group, tag, ci, q, include_closed, limit, offset) | api:read | `list_records` | `api/__init__.py:133-153` |
| POST `/records` | api:write (source/external_id: itsm:admin) | `create_record` | `:155-171` |
| GET `/records/{ref}` | api:read | `get_record` | `:173-179` |
| PATCH `/records/{ref}` (mit `version`) | api:write | `update_record` | `:181-191` |
| POST `/records/{ref}/transition` | api:write | `transition` | `:193-202` |
| POST `/records/{ref}/journal` (`kind` Default comment) | api:write | `add_journal` | `:204-210` |
| POST `/records/{ref}/ci-links`, DELETE `.../ci-links/{role}/{entity_uuid}` | api:write | `link_ci`/`unlink_ci` | `:213-232` |
| POST `/records/{ref}/links`, DELETE `.../links/{relation}/{to}` | api:write | `link_records`/`unlink_records` | `:234-248` |
| POST/GET/DELETE `/records/{ref}/attachments[/{uuid}]` | write/read/write | `add/get/delete_attachment` | `:251-284` |
| GET `/catalog`, POST `/catalog/reload` | read / itsm:admin | `catalog.to_payload`, `reload_catalog` | `:287-300` |
| GET `/stats` | api:read | `get_stats` | `:302-305` |

Fehlermapping: ItsmError 400, NotFound 404, VersionConflict/TransitionError 409, CatalogError 400 (`:111-123`). Kein `/settings`-Pfad (reserviert, ok).
`list_records(ci=<entity_uuid>, record_type="incident", state=...)` liefert die Kandidaten für Dedupe (`service.py:487-488`), liefert aber volle
`to_dict`s, kein Limit auf Treffer ohne `limit`-Parameter (Default 200, Sortierung `opened_at desc`, `:492`).

## 6. UI-Komponenten (`app/ui/react/PluginApp.tsx`)

- `PluginApp` (Root, `:947-993`): Deep-Links aus `window.location.search` (`:956-963`): `?record=<uuid|number>` -> Detail, `?new=<type>&ci=<uuid>` -> Create,
  `?ci=<uuid>` -> gefilterte Liste. Views: list/create/detail/catalog.
- `RecordList` (`:275`, CI-Filter `:284`, Filter-Chip `:337`), `StatsStrip` (`:258`), `StateBadge` (`:66`, Farbe nach State-Kind),
  `PriorityBadge` (`:73`).
- `CreateForm` (`:388`): `initialCi` wird über `registryApi.get` aufgelöst (`:405-420`), Rolle fest `affected`; Submit sendet `ci_links` (`:426`, `:440`).
- `CiPicker` (`:154`), CI-Panel im Detail (`:829-847`: Liste mit Link "Records für dieses CI" `:839`, Unlink `:843`, Picker `:847`).
- `TransitionModal` (`:559`), `RecordDetailView` (`:632`), `CatalogView` (`:900`). API-Client `app/ui/react/lib/api.ts`.
- Journal-Eintrag mit `author = "monitoring"` erscheint ohne UI-Änderung in der Timeline (generisch aus `itsm_journal`).
- Locales `locales/itsm.{de,en}.json`; neue UI-Texte (Quelle "Monitoring", Hinweis auf Auto-Resolve) müssten in beiden Dateien ergänzt werden (i18n-Parität, `check_i18n_keys.py` im Core).
- Benachrichtigungen (Backend): `ctx.notify` in `_notify` (`entrypoint.py:362-370`); Endpoints `record_assigned`, `record_state_changed`,
  `record_commented` (`:142-149`); `state_changed` mit Severity `warning` bei P1/P2 (`:384`). **`itsm:record_created` löst keine Notification aus**
  (kein Subscriber), d. h. ein neuer Incident mit Assignee `None` erzeugt keinen Glocken-Eintrag, nur `itsm:record_created` auf dem Bus.

## 7. Teststand

- `tests/conftest.py` (importiert `core.services` vor `core.api`, Zirkelimport-Workaround, `:1-9`); `tests/test_service.py`: 37 Tests.
- Fixture `svc` (`test_service.py:25-44`): In-Memory-SQLite (`StaticPool`, `PRAGMA foreign_keys=ON`), `_FakeCtx` mit `emit()`-Recorder und `cluster_topics` (`:12-22`);
  `_RegistryCtx`/`_registry()` simuliert `ctx.request` (`:341-370`).
- Abgedeckt u. a.: Katalog (`:53`, `:60`), Nummern/Priorität (`:75`, `:90`), Übergänge/Pflichtfelder (`:128`), Resolve/Close/Reopen (`:140`),
  CI-Links (`:115`, `:256`, `:375-418`), `(source, external_id)`-Eindeutigkeit (`:318`), Cluster-/Retry-Pfade (`:539-707`).
- **Nicht getestet**: Bus-Handler in `entrypoint.py` (kein Test importiert `entrypoint`), Notifications, REST-Router außer 409-Mapping (`:707`).
- Ausführung (`documentation.md:249-262`): `docker cp . lyndrix-core-dev:/tmp/itsm` und `docker exec -w /tmp/itsm -e PYTHONPATH=/app:/tmp/pytest-sm lyndrix-core-dev python -m pytest -q tests`;
  im HA-Bench zusätzlich `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local` (CLAUDE.md). Lint: `ruff check entrypoint.py app tests`.

## 8. Cluster-Besonderheiten

- Kein `placement`, kein `on_lease`, kein `ctx.lease`, kein `is_leader` im Plugin (Grep über `entrypoint.py` und `app/` leer).
  Alle Instanzen führen Routen und Bus-Handler aus.
- Vorhandene Schutzmechanismen: optimistisches Locking per `version` (`service.py:247-283`), Counter-Tx (`:214-246`), Retry bei 1020/1205/1213 (`database.py:66-79`),
  Bootstrap-Lock (`database.py:82-87`), Katalog-Reload per `cluster=True` (`service.py:175`), Settings-Reapply auf jeder Instanz (`entrypoint.py:223-226`).
- **Kein Guard gegen Doppelanlage durch Bus-Events**: Ein mit `cluster=True` emittiertes `monitoring:outage_persisted` erreicht jede Instanz
  (Origin-Marker `payload["_origin"]`, CLAUDE.md); jede ITSM-Instanz würde `create_record` ausführen. Es gibt keinen Check "offener Incident je ci_id".
- Einziger Idempotenz-Mechanismus: `UNIQUE(source, external_id)` (`models.py:46`) plus Vorab-Select und IntegrityError-Mapping (`service.py:411-414`, `:444-449`).
  Dieser fängt gleichzeitige Anlagen mit identischem Schlüssel sicher ab (auch über Instanzen hinweg, DB-seitig).
- Anhänge brauchen RWX-Storage; irrelevant für die Funktion, aber `health()` meldet `degraded` bei nicht geteiltem Verzeichnis (`service.py:933-1015`).

## 9. Einhängepunkte für Plan 7.3

1. **Subscription + Permissions** - `entrypoint.py:110-125` ergänzen: `monitoring:outage_persisted`, `monitoring:outage_recovered`
   (optional `maintenance:override_started`). Handler analog `_on_record_create` (`:261-277`) in `setup()` anlegen; Logik in `app/logic/` auslagern
   (`entrypoint.py` bleibt dünn): neue Datei z. B. `app/logic/monitoring_incidents.py` mit Service-Methode `handle_outage_persisted(payload)` / `handle_outage_recovered(payload)`.
   Risiko: Payload-Schema kommt von der Monitoring-Analyse (Plan 6/7.2) - Felder (`ci_id`, `host_ci_id`, `rule_id`, `outage_id`, `severity`, `auto_resolve`, `since`, `monitor`) vorab festschreiben.
2. **Incident anlegen** - `ItsmService.create_record` direkt nutzen (nicht über den Bus-Umweg `itsm:record_create`). Felder: `record_type="incident"`,
   `subtype="outage"` (`incident.yml:15`), `category` je CI-Typ (`hypervisor|vm|container|application`, `:23-36`) oder `monitoring` (`:40`),
   `title` mit Servicename, `description` mit Dauer/Monitor/Regel, `tags=["auto","monitoring"]`, `requester`/`author="monitoring"`.
   CI-Links vorab über `resolve_ci_links` (`service.py:721`) oder, weil `ci_id` bereits eine Registry-UUID ist, direkt als `{"entity_uuid", "role": "affected"}` (Service-CI) und
   `{"role": "related"}` (Host-CI); `resolve_ci_links` ruft pro UUID die Registry mit Timeout 5 s auf (`:740`), bei Registry-Ausfall fällt es auf den rohen UUID-Link zurück (`:745`) - für Alarmpfade ausreichend.
   `_check_ci_links` erzwingt 36-stellige `entity_uuid` (`:667`). Rollen laut `CI_LINK_ROLES` (`catalog.py:46`), Host-CI also `related` oder `affected`.
3. **Severity-Mapping** - Kein Severity-Feld; Mapping auf `impact`/`urgency` nötig, damit `priority` per Matrix (`incident.yml:54-63`) herauskommt:
   `critical -> impact 1, urgency 1 (P1)`, `high -> 1/2 (P2)`, `medium -> 2/2 (P3)`, `low -> 3/2 (P4)` (Vorschlag; Matrix gibt diese Werte her).
   Mapping als Setting (`entrypoint.py:98-108`, Muster `settings_schema`) oder Konstante in der neuen Logikdatei. Erlaubte Werte `impact` 1-3 / `urgency` 1-3 (`incident.yml:44-51`, Prüfung `service.py:360-373`).
4. **Dedupe (ein offener Incident je ci_id [+ Regel])** - Noch nicht vorhanden. Zwei Varianten:
   a) *Idempotenzschlüssel*: `source="monitoring"`, `external_id=f"{rule_id}:{ci_id}"`; der Unique-Constraint verhindert Parallelanlagen sicher auch cluster-weit
   (`service.py:411-414`, `:444-449`). Nachteil: Schlüssel bleibt nach Resolve belegt -> ein erneuter Ausfall später würde mit "already exists" scheitern;
   `external_id` müsste die `outage_id` enthalten (dann Dedupe je Ausfall, nicht je CI) oder der alte Datensatz müsste wiederverwendet/reopened werden
   (`resolved -> in_progress` ist erlaubt, `incident.yml:84`; `closed` ist final). Zudem markiert `source` den Record als extern geführt (Semantik `models.py:78-79`, REST-Admin-Gate `api/__init__.py:163`).
   b) *Abfrage*: neue Service-Methode `find_open_for_ci(entity_uuid, record_type="incident", tag/rule)`: Join `Record` x `CiLink` (`CiLink.entity_uuid == ci`, `role == "affected"`) mit
   `Record.state` in Open-States (State-Kind `open`, Katalog `incident.yml:73-75`) - **nicht** `closed_at IS NULL` (schließt `resolved` ein, s. Abschnitt 3). Vorbild: `list_records(ci=...)` (`service.py:487-488`).
   Ohne DB-Constraint ist (b) nicht atomar (Race zwischen zwei Instanzen) - daher (b) nur mit Single-Writer (Punkt 6) oder kombiniert mit (a).
   Empfehlung zur Prüfung: (b) für Wiederöffnung/Kommentar bei Wiederholung ("Kommentar statt neuer Incident" via `add_journal`) plus Single-Writer.
5. **Recovered -> Auto-Resolve oder Kommentar** - Offenen Incident per Dedupe-Query finden, Regelwahl `auto_resolve` aus Payload:
   - true: `transition(ref, "resolved", fields={"resolution_code": "fixed", "resolution_notes": "Automatisch: Monitor wieder erreichbar nach <Dauer>"}, note=..., author="monitoring")`
     (`service.py:569`; Pflichtfelder `incident.yml:76`). Aus `on_hold` ist `resolved` **nicht** erlaubt (`:83`: nur `in_progress|cancelled`) -> Handler muss `TransitionError` abfangen und auf Kommentar zurückfallen.
     Ein bereits `resolved` Incident bleibt unberührt. Schließen (`closed`) bleibt manuell oder später per Regel.
   - false: `add_journal(ref, "comment", "...", author="monitoring")` (`service.py:644`). `comment` löst Benachrichtigung `record_commented` aus (`entrypoint.py:392`), `work_note` nicht.
   Fehler werden im bestehenden Muster nur geloggt/`itsm:ingest_failed` (`entrypoint.py:249-251`). Version-Konflikt: ohne `expected_version` wird einmal wiederholt (`service.py:260-283`).
6. **Cluster-Single-Writer** - Heute fehlend. Optionen: (i) `placement`/`on_lease` (`ctx.on_lease("itsm:monitoring-incidents", acquired=..., lost=...)`, `getattr`-geschützt, CLAUDE.md "Cluster mode") - nur die Lease-Instanz
   verarbeitet die Monitoring-Events (Handler prüft `ctx.is_leader`-Äquivalent/Lease-Flag; Events mit `cluster=True` erreichen alle Instanzen); `ctx.release_lease` nicht nötig. (ii) kurze `async with ctx.lease("itsm:outage:<ci_id>")` je Event.
   (iii) DB-Constraint (Punkt 4a). Risiko: Ein Lease-Wechsel zwischen Persisted und Recovered verliert nichts, solange der Zustand ausschließlich in der DB liegt (ist der Fall, kein In-Process-State).
   Manifest-Permissions für Lease-Topics prüfen; Core >= 1.7 nötig, Fallback bei älterem Core: direkt starten (`getattr`-Muster).
7. **System-Actor / Berechtigungen** - `author` ist freier String; Konvention `"monitoring"` (analog Default `"bus"`, `entrypoint.py:268`). `requester` fällt auf `author` zurück (`service.py:423`).
   Bus-Pfad umgeht `itsm:admin`-Gate für `source/external_id` (`entrypoint.py:268-274` vs. `api/__init__.py:163`); das ist für den internen Aufruf gewollt, aber ein Sicherheitsrisiko, falls andere Plugins `itsm:record_create` emittieren dürfen - Core prüft `permissions.emit` pro Plugin.
8. **Notifications** - `itsm:record_created` hat keinen Subscriber; für Ausfall-Incidents (P1/P2) sollte `_notify` (`entrypoint.py:362`) erweitert werden (neuer Endpoint in `notification_endpoints` `:142-149`, z. B. `record_created`, Subscribe auf eigenes `itsm:record_created`
   in `permissions.subscribe`). Ohne diese Erweiterung bekommen Operatoren erst bei Zuweisung/Statuswechsel/Kommentar eine Meldung. `state_changed` bei Auto-Resolve meldet bereits (`:381-387`).
9. **UI** - Keine Pflichtänderung. Optional: Tag/Badge "auto" und Filter in `RecordList` (`PluginApp.tsx:275`), Deep-Link `?ci=<uuid>` existiert (`:956-963`) und zeigt alle Records eines CI (inkl. Incidents für Wartungsfenster-Ansicht).
10. **Zweite Stufe `maintenance:override_started` aus Change** - Change-Katalog hat `planned_start`/`planned_end` als Pflichtfelder ab `approval` (`change.yml:63`). Hook: Handler auf `itsm:state_changed` (`entrypoint.py:381`) mit
    `record_type == "change"` und Zielstate (z. B. `scheduled`/`implement`, in `change.yml:60-70` nachsehen) -> CI-Links Rolle `changed` (`catalog.py:46`) auslesen und `maintenance:override_started` emittieren (Topic in `permissions.emit` ergänzen). Nicht Teil des ersten Wurfs.
11. **Tests ergänzen** (`tests/test_service.py`): Fixture `svc`/`_RegistryCtx` nutzen (`:25`, `:341`); Fälle: Anlage mit Severity-Mapping, Dedupe (2x persisted -> 1 Record), Recovered auto_resolve true/false, `on_hold` -> Fallback-Kommentar, Resolved-Incident wird nicht erneut aufgelöst, erneuter Ausfall nach Resolve. Handler in `entrypoint.py` sind ungetestet - Logik daher in `app/logic/` platzieren.
12. **Versionierung** - Manifest `0.2.0 -> 0.3.0` (`entrypoint.py:76`), Tag passend (Memory: nächste Minor), `CHANGELOG.md` und `documentation.md` (Deutsch) ergänzen, Locales de/en.

## 10. Abweichungen vom Plan / offene Fragen

- Plan 7.3 spricht von "Severity aus der Regel" (`critical|high|medium|low`): ITSM kennt nur `impact`/`urgency` -> Prioritätsmatrix P1-P4 (`incident.yml:43-68`). Mapping-Tabelle muss festgelegt werden (Vorschlag Punkt 3).
- Plan: "Dedupe: offener Incident je `ci_id`". Im Code existiert nichts davon; "offen" ist kein Katalogfilter (`closed_at IS NULL` zählt `resolved` mit, `service.py:476`). Frage: Soll ein `resolved` Incident bei erneutem Ausfall wiederverwendet (`resolved -> in_progress` möglich) oder ein neuer angelegt werden? Dedupe je `ci_id` allein oder je `ci_id` + Regel (zwei Regeln auf einem CI)?
- Plan: "Service- und Host-CI-Links": Rollenwahl offen (`affected` für Service, `related` oder `caused_by` für Host?). Rollen sind auf vier fixiert (`catalog.py:46`); Host als Ursache ist erst bei Root-Cause-Analyse sinnvoll.
- Auto-Resolve aus `on_hold` ist laut Katalog nicht erlaubt (`incident.yml:83`); soll der Katalog `on_hold -> resolved` erlauben, oder gilt Fallback-Kommentar? Plan sagt "wenn Regel es erlaubt, sonst Kommentar" - der Fall `on_hold` ist nicht abgedeckt.
- Resolve verlangt `resolution_code` + `resolution_notes` (`incident.yml:76`); für Auto-Resolve muss ein Code festgelegt werden (Vorschlag `fixed`, evtl. neuer Code `auto_recovered` im Katalog; dann Katalog-Update `incident.yml:96-104`).
- Cluster: Plan 6 sagt "alles fire-and-forget"; ob `monitoring:outage_*` mit `cluster=True` emittiert wird, entscheidet die Monitoring-Analyse (dort Lease `monitoring:prober`). Wird nur lokal emittiert und nur der Prober-Leader sendet, reicht ITSM-seitig das DB-Constraint; sonst Lease nötig (Punkt 6).
- Kopplungsregel (CLAUDE.md): "Kein Plugin darf ein anderes zwingend brauchen" - ITSM fehlt -> Monitoring darf nicht scheitern (fire-and-forget, ok); Monitoring fehlt -> ITSM ohne Funktion, aber lauffähig. Ein Request/Reply (`ctx.request`) für Incident-Nummer zurück an Monitoring wäre optional, Antworttopic müsste in beide Manifeste.
- Fehlende Notification bei `itsm:record_created` (Abschnitt 6) - gewollt oder nachziehen?
- `Record` hat keinen Verweis auf Outage/Regel: Soll `custom_fields` eine Katalogerweiterung (`incident.yml` `fields`, z. B. `monitor_rule`, `outage_id`) bekommen (kein Code nötig, `catalog.py` validiert Custom-Felder, `service.py:400`)?
