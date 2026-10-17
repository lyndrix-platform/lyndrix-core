# Analyse: Entity Registry (Sweep zu Plan 7.4 / Abschnitt 10)

Repo: `/home/marvin/gitlab/lyndrix-dev/plugins/lyndrix-plugin-entity-registry` (Stand: Version 0.2.0).
Alle Pfade relativ zum Repo, sofern nicht anders angegeben. Rein lesende Analyse, kein Code geändert.

## 1. Überblick

| Punkt | Wert | Beleg |
|---|---|---|
| Manifest-ID | `lyndrix.plugin.entity_registry` | `entrypoint.py:68` |
| Version | `0.2.0` | `entrypoint.py:75` |
| `min_core_version` | `1.4.0` (kein `placement` gesetzt, also Default `any`) | `entrypoint.py:83` |
| `auto_enable_on_install` | `True` | `entrypoint.py:84` |
| UI | `react_ui=True`, Route `/registry` (sidebar_visible), `i18n_namespace="entity_registry"` | `entrypoint.py:86-92` |
| Rollen | `admin` (api:read+write, auto_map `INT_ADMIN`), `viewer` (api:read) | `entrypoint.py:116-125` |
| `permissions.subscribe` | `db:connected`, `entity_registry:{identify,register,lookup,relate,unrelate,retire}`, `entity_registry:catalog_reloaded` | `entrypoint.py:94-103` |
| `permissions.emit` | `entity_registry:{identified,lookup_result,entity_created,entity_updated,entity_merged,entity_retired,conflict,ingest_failed,catalog_reloaded}` | `entrypoint.py:104-114` |
| Schichten | `app/model` (Modelle, DB-Helfer), `app/logic` (catalog, service), `app/api`, `app/ui/react`; kein `app/ui/nicegui` | Dateiliste, `entrypoint.py:7-15` |
| AI-Instructions | nicht vorhanden (kein `ai/`, kein `ai_instructions*` im Manifest) | grep ohne Treffer |

Das Plugin liest/schreibt nur die eigene DB (MariaDB, SQLAlchemy). Es hat keine Hintergrund-Tasks, keine Leases, keinen Scheduler.

## 2. Datenmodell

### 2.1 Tabellen (`app/model/models.py`)

| Tabelle | Spalten (Auszug) | Zeile |
|---|---|---|
| `registry_entities` | `uuid` PK(36), `entity_type` String(64) idx, `display_name`, `status` (`active\|retired\|merged`) idx, `owner_source`, `sources` JSON (Liste "gesehen von"), `attributes` JSON (dict), `merged_into`, `created_at`, `updated_at`, `last_seen_at` | `models.py:34-67` (entity_type :38, status :41, sources :45, attributes :47, merged_into :49) |
| `registry_identifiers` | `id`, `entity_uuid` FK, `scheme`, `value`, `source`, `created_at`; **UNIQUE (scheme, value)** | `models.py:70-91` (Unique :73) |
| `registry_relations` | `id`, `from_uuid` FK, `to_uuid` FK, `relation` String(64), `source`, `created_at`; UNIQUE (from,to,relation) | `models.py:94-119` (Unique :97, relation :107) |
| `registry_conflicts` | `entity_type`, `fingerprint`, `open_fingerprint` (UNIQUE-Index, NULL nach Abschluss), `candidates` JSON, `identifiers` JSON, `source`, `status` (`open\|resolved\|dismissed`), `resolution`, Zeitstempel | `models.py:122-` |

Es gibt **keine** separate "Source-Records"-Tabelle und **keine** Attribut-Tabelle. "Candidates/Merge-Proposals" = `registry_conflicts` (Merge nie automatisch, `service.py:3-7`, `:456-466`). Merge hinterlässt Tombstone (`status="merged"`, `merged_into`), alte UUIDs lösen weiter auf (`service.py:145-154`, `:744-745`).

### 2.2 Kind-Vokabular (`entity_type`)

- **Definition:** ausschließlich YAML `catalog/entity_types.yml:14-` (kein Python-Enum). Validiert beim Laden per Pydantic (`app/logic/catalog.py:34-39` Regex `^[a-z][a-z0-9_]*$`, Unique-IDs `:57-66`). Unbekannter Typ bei identify/register → `RegistryError` (`service.py:127-130`).
- **Exakte Liste (7):** `host`, `hypervisor`, `service`, `container`, `network`, `itsm_record`, `generic` (`entity_types.yml:15,19,23,27,31,35,39`).
- **`vm` existiert NICHT.** Eine VM/ein LXC ist `host` ("Virtual machine or LXC container managed as a server", `entity_types.yml:15-18`); ein physischer Node ist `hypervisor`. Der Orchestrator setzt nur `host` oder `hypervisor` (`lyndrix-plugin-iac-orchestrator/app/controller/engine.py:1189`).
- **Tatsächlich genutzt:** Orchestrator -> `host`/`hypervisor` (`engine.py:1189,1221`); ITSM -> `itsm_record` (`lyndrix-plugin-itsm/app/logic/service.py:1028`). Ich habe im Plugin-Verzeichnis **keinen** Emitter gefunden, der `service`- oder `container`-Entities anlegt (grep nach `entity_type` mit service/container/vm in `plugins/**/*.py` ohne Treffer). Das Vokabular existiert, die Daten dazu noch nicht.
- Katalog ist per `POST /catalog/reload` hot-reloadbar (`api/__init__.py:264-273`), Cluster-Announce via `entity_registry:catalog_reloaded` (`service.py:118-123`).

### 2.3 Identifier-Schemes (`entity_types.yml:44-76`)

Lower priority number gewinnt bei Konflikten: `pve.vm` 10 (Format `<node>/<vmid>`), `itsm.number` 10, `bmc.reconid` 10, `iac.host` 20, `fqdn` 30, `docker.container` 40 (`<host>/<name>`), `server_manager.id` 50, `monitoring.monitor` 60, `external` 90. IPs sind bewusst **keine** Schemes (`entity_types.yml:6-9`). Unbekanntes Scheme -> `RegistryError` (`service.py:132-136`). Normalisierung: trim, leere/None verworfen (`service.py:60-68`).

### 2.4 Attribute / Erweiterbarkeit

- `Entity.attributes` ist **eine JSON-Spalte** `dict` (`models.py:47`), "Weak / descriptive facts ... Never used for matching" (`models.py:46`).
- **Kein Namespacing pro Quelle.** Schlüssel sind flach und frei; jeder Schreiber (identify/register/PATCH) kann jeden Schlüssel setzen. `sources` ist eine eigene Liste, nicht pro Attribut (`service.py:512-514`).
- **Schreibpfade:**
  - `identify`/`register` -> `_absorb` bzw. `_create`: **flacher Merge** `{**alt, **neu}`, neue Werte überschreiben (`service.py:507-511`); tagged `changes` -> `entity_updated`.
  - `PATCH /entities/{uuid}` -> `update_entity(attributes, replace_attributes)`: Merge oder Replace (`service.py:668-672`, `api/__init__.py:110-124`), Permission `api:write`.
  - Bus: `identify` mit `attributes` (`entrypoint.py:188-195`).
  - Merge zweier Entities: `{**drop, **keep}`, keep gewinnt (`service.py:738`).
- **Read:** `GET /entities/{uuid}` -> `{"entity": detail}` mit `attributes` als dict (`models.py:60`-`to_dict`, `service.py:156-190`, `api/__init__.py:102-108`). `GET /entities` (Liste) liefert dieselben `to_dict()`-Felder + `identifiers`, **keine** Relationen (`service.py:233-236`).
- Typisches Beispiel aus Produktion: Orchestrator schickt `ip, site, stage, groups` als Attribute (`engine.py:1213-1219`).
- Kein Schema/Whitelist/Größenlimit auf `attributes` (Pydantic `Dict[str, Any]`, `api/__init__.py:31`).

### 2.5 Relationen

- **Ja, ein Relationsmodell existiert:** `registry_relations` (gerichtet, gelabelt), Katalog-Labels `runs_on`/`hosts`, `depends_on`/`required_by`, `member_of`/`has_member`, `affects`/`affected_by`, `related_to` (`entity_types.yml:77-102`). Inverse sind nur im Katalog deklariert (`catalog.py:94-98`); es wird **nur die eine Richtung** gespeichert (`service.py:778`), `_entity_detail` liefert eingehende und ausgehende Kanten mit `direction` + `other_uuid/other_display_name/other_entity_type` (`service.py:166-189`).
- Schreiben: `relate()` (`service.py:761-785`; Bus `entity_registry:relate` `entrypoint.py:275-293`; REST `POST /entities/{uuid}/relations`).
- **Tatsache:** kein Plugin im Repo-Baum ruft `entity_registry:relate` auf (grep `entity_registry:relate` außerhalb des Registry-Repos ohne Treffer). Die Relationstabelle ist also im Betrieb vermutlich leer; `runs_on` Service->Host wird heute von niemandem gepflegt. Der Orchestrator-Publish-Pfad (`engine.py:1192-1260`) macht nur `identify` für Hosts.
- Konsequenz: Ein Konsument findet den Host-CI eines Service-CI **heute nur**, wenn jemand `service --runs_on--> host` anlegt. Alternative Hinweise: `attributes` (frei, z. B. `site`, `stage`) und `docker.container`-Identifier `<host>/<name>` (der Host-Teil ist ein `iac.host`-Key, `entity_types.yml:62-65`).

## 3. Verträge

### 3.1 `entity_registry:identify` -> `entity_registry:identified` (`entrypoint.py:197-226`)

Request (Einzel): `{entity_type, identifiers:{scheme:value}, display_name?, attributes?, source, request_id?}` (Pydantic `IdentifyPayload`: `entity_type`, `identifiers` min 1, `source` 1-64 Pflicht; `api/__init__.py:27-32`).
Request (Batch): `{items:[IdentifyPayload...], request_id?}` (Batch-Items nicht auf 1000 begrenzt im Bus-Pfad, REST `max_length=1000`, `api/__init__.py:36`).
Reply nur wenn `request_id` gesetzt (`entrypoint.py:223-226`, sonst bei Fehler `entity_registry:ingest_failed`):
- Einzel: `{request_id, ok:true, uuid, created:bool, conflict:bool}` (`entrypoint.py:194-195`)
- Batch: `{request_id, ok:true, results:[{ok:true, uuid, created, conflict} | {ok:false, error, identifiers}]}` (`entrypoint.py:207-216`), Reihenfolge = Request-Reihenfolge (Orchestrator nutzt `zip(hosts, results)`, `engine.py:1237`).
- DB nicht bereit: `{request_id, ok:false, error:"database not ready"}` (`entrypoint.py:205-206`).
Semantik (`service.py:377-466`): find-or-create. Kein Treffer -> neu (`created:true`). Genau ein Treffer -> Aliase/Name/Attribute/Quelle einarbeiten (`_absorb`). Mehrere -> `conflict:true`, Rückgabe-UUID = Entity mit stärkstem Scheme (`:457-460`), nur Conflict-Datensatz wird geschrieben. Typ-Mismatch wird nur geloggt (`:442-447`).
Aufruf-Muster der Konsumenten: `ctx.request("entity_registry:identify", {"items": items}, reply_topic="entity_registry:identified", timeout=10)` (`engine.py:1225-1228`); `ok=false`/`None` = Registry fehlt -> ohne `ci_id` weiter (`:1229-1231`).

**Wichtig für Wartung:** `identify` **legt Entities an**. Wer ein Event nur dann emittieren will, wenn die `ci_id` existiert, soll `lookup`/`resolve` nutzen (read-only), nicht `identify`.

### 3.2 `entity_registry:lookup` -> `entity_registry:lookup_result` (`entrypoint.py:246-273`)

Request: `{ref | uuid | (scheme + value), entity_type?, request_id}`; `scheme`+`value` werden zu `ref="scheme:value"` (`:255-257`).
Reply (nur bei `request_id`): `{request_id, ok:true, entity: detail|null, match: "uuid"|"identifier"|"display_name"|"ambiguous"|null, candidates:[summary...]}` (`:267`, `service.py:251-312`). Fehler: `ok:false, error` ("ref ... required", "database not ready", `:259-261`).
`resolve()` Auflösung der `ref` (`service.py:269-312`):
1. UUID-Regex -> Tombstones folgen; `entity_type`-Filter optional; sonst `entity:null`, `candidates:[]` (`:270-274`).
2. `scheme:value`, **nur** wenn Prefix ein Katalog-Scheme (damit FQDN/`hydra/8003` nicht falsch zerlegt werden, `:276-282`); sonst nackter Wert gegen alle Schemes.
3. Treffer nur `status=="active"` (retired/merged-Ziele werden verworfen, `:286-287`). 1 Treffer -> `match:"identifier"`; >1 -> `entity:null`, `match:"ambiguous"`, max. 5 `candidates` (`:291-296`).
4. Display-Name case-insensitiv, gleiche 1/>1-Logik (`:298-309`).
5. Sonst `entity:null, match:null`, `candidates` = Teilstring-Treffer (`:311-312`).
`entity` ist das volle `_entity_detail` (inkl. `identifiers`, `relations`, `attributes`).

### 3.3 REST `GET /resolve?ref=&type=` (`api/__init__.py:202-212`)
Immer 200 mit demselben Körper wie `resolve()`; `ref` 1-255 Zeichen, Permission `api:read`. `GET /lookup?scheme=&value=` liefert dagegen 404 bei Nichttreffer (`:192-200`).

### 3.4 Weitere Bus-Verträge
- `entity_registry:register` (BYO-UUID, idempotent, fremd besessene Identifier -> Fehler) `entrypoint.py:228-244`, `service.py:565-637`. Von ITSM genutzt.
- `relate`/`unrelate`/`retire`: `entrypoint.py:275-317`; Fehler nur per `ingest_failed` bzw. Log, **keine Antwort-Events**.

## 4. Events

| Richtung | Topic | Payload | Beleg |
|---|---|---|---|
| emit | `entity_registry:entity_created` | `{entity}` (Detail inkl. identifiers, relations) | `service.py:433,595` |
| emit | `entity_registry:entity_updated` | `{entity, changes:[...]}`; Tags: `identifier:<scheme>`, `display_name`, `attributes`, `sources`, `status`, `type_mismatch`, `relations` | `service.py:451,597,656,784` |
| emit | `entity_registry:entity_merged` | `{keep, drop_uuid}` | `service.py:700` |
| emit | `entity_registry:entity_retired` | `{entity}` | `service.py:689` |
| emit | `entity_registry:conflict` | `{conflict}` (nur beim ersten Anlegen) | `service.py:464` |
| emit | `entity_registry:ingest_failed` | `{topic, error, payload_keys}` | `entrypoint.py:185` |
| emit (cluster) | `entity_registry:catalog_reloaded` | `{reloaded_at}` | `service.py:123` |
| emit | `identified`, `lookup_result` | Antworten (s. 3) | `entrypoint.py:224,271` |
| subscribe | `db:connected`, `identify`, `register`, `lookup`, `relate`, `unrelate`, `retire`, `catalog_reloaded` | | `entrypoint.py:166-170,197-308` |

Hinweise:
- **Es gibt aktuell keinen Subscriber auf `maintenance:*`.** Neue Topics müssen in `permissions.subscribe` stehen (`entrypoint.py:94-103`).
- Emits aus `to_thread`-Workern werden auf die Loop zurückgemarshallt (`service.py:829-847`).
- `entity_updated` wird auch bei reinen Attribut-Änderungen emittiert (`service.py:656`) und enthält das **komplette** Entity-Detail -> ein Maintenance-Write würde pro Entity ein vollständiges Detail-Event erzeugen (Volumen bei `windows_published` mit vielen Items beachten, siehe 9).
- Alle Bus-Handler sind lokal (nur die Instanz, die das Event empfängt); nur `catalog_reloaded` wird mit `cluster=True` gesendet bzw. von Peers (`payload["_origin"]`) verarbeitet (`entrypoint.py:170-179`).

## 5. API-Routen

Prefix `/api/plugins/lyndrix.plugin.entity_registry/` (`api/__init__.py:3`). Alle Handler: `_ensure_ready()` (503 wenn Tabellen nicht bereit) und `asyncio.to_thread`; `RegistryError`->400, `NotFound`->404 (`api/__init__.py:72-85`). Permissions `plugin:<id>:api:read|write` (`:21-22`).

| Methode | Pfad | Perm | Zeile |
|---|---|---|---|
| GET | `/entities?type=&q=&status=active\|retired\|all&limit(1-1000,200)&offset` | read | :88-100 |
| GET | `/entities/{uuid}` -> `{entity}` (folgt Tombstones) | read | :102-108 |
| PATCH | `/entities/{uuid}` (`display_name`, `attributes`, `replace_attributes`) | write | :110-124 |
| POST | `/entities/{uuid}/retire` | write | :126-134 |
| POST | `/entities/{uuid}/merge` (`drop_uuid`) | write | :136-144 |
| POST | `/identify` | write | :147-155 |
| POST | `/identify/batch` (max 1000) | write | :157-179 |
| POST | `/register` | write | :181-190 |
| GET | `/lookup?scheme=&value=` (404 bei Nichttreffer) | read | :192-200 |
| GET | `/resolve?ref=&type=` | read | :202-212 |
| POST | `/entities/{uuid}/relations` (`to_uuid`, `relation`) | write | :215-225 |
| DELETE | `/entities/{uuid}/relations/{relation}/{to_uuid}` | write | :227-237 |
| GET | `/conflicts?status=` | read | :240-245 |
| POST | `/conflicts/{id}/dismiss` | write | :247-257 |
| GET | `/catalog` | read | :260-262 |
| POST | `/catalog/reload` | write | :264-273 |
| GET | `/stats` | read | :275-278 |

Keine Route nutzt `/settings` (reservierter Pfad). Keine Batch-Lese-Route (kein `GET /entities?uuids=`), kein `settings_schema`.

## 6. UI-Komponenten (`app/ui/react/PluginApp.tsx`, 562 Zeilen)

| Komponente | Zeile | Inhalt |
|---|---|---|
| `TypeBadge` | :41-49 | Typ-Badge aus Katalog (Icon/Label) |
| `StatusBadge` | :51-60 | `active/retired/merged` (`lx-badge--up/paused/muted`) |
| `IdentChip` | :62-68 | Identifier-Chip |
| `StatsStrip` | :110-131 | Zähler pro Typ, Konflikt-Button |
| `EntityList` | :133-225 | Tabelle Name/Typ/Identifiers/Seen by/Last seen. **Typ-Spalte** :207 (`<TypeBadge/> + StatusBadge` wenn nicht active) |
| `EntityDetail` | :227- | Kopfzeile :279-288 (`h2` + `TypeBadge` + `StatusBadge`); Karte Identity :292-315; Karte **Attributes** :317-332 (rendert alle `attributes` als Key/Value-Tabelle, Nicht-Strings via `JSON.stringify` :327); Relations :334-365; ITSM-Karte :367-393; Actions :395- |
| `ConflictList` | ~:455-515 | Konfliktliste/Merge |
| `PluginApp` (Root) | :508-562 | Deep-Link `?uuid=<36 Zeichen>` -> Detailansicht :523-528 |

Typen: `Entity.attributes: Record<string, unknown>` (`app/ui/react/lib/api.ts:36`), `Entity` Interface ab :27. API-Client `registryApi.listEntities` :124, `getEntity` :133. Kein `panel`-Prop-Support, keine Panels.
Das gebaute Bundle `app/ui/static/ui_bundle.js` ist im Git getrackt (Build per `vite.ui.config.ts`) -> nach UI-Änderung neu bauen und mitcommitten.
i18n: `locales/entity_registry.{de,en}.json`, im Code zusätzlich `defaultValue`-Fallbacks (z. B. `PluginApp.tsx:57`).

## 7. Teststand

- `tests/test_registry.py` (474 Zeilen, ~30 Tests) gegen **In-Memory-SQLite** mit `PRAGMA foreign_keys=ON` und `StaticPool`; Fixture `svc` baut `EntityRegistryService` + `_FakeCtx` (sammelt `events`, `cluster_topics`) (`tests/test_registry.py:12-55`).
- Abgedeckt: identify (neu/erkannt/Rename via stärkerem Identifier/Attribute-Merge/Konflikt/Validierung), merge + Tombstone, register-Idempotenz, Relationen, retire/resurrect, Liste/Suche/Stats, Katalog-Duplikate, `resolve` (UUID/Scheme/Name/ambiguous/Tombstone/unbekannt), Bootstrap-Lock + Race, Retry bei 1020, Identify-Insert-Race, `last_seen`-Drosselung, Unique-Open-Conflict, Catalog-Reload cluster-weit, Health-Probe.
- **Nicht abgedeckt:** die Bus-Handler in `entrypoint.py` (`_on_identify`, `_on_lookup`, ...), die REST-Router (`app/api`), die React-UI. Kein Test für Attribute-Konkurrenz.
- `tests/conftest.py:9` importiert `core.services` vor `core.api` (zirkulärer Import, `TODO(agent)` im Core).
- Ausführen: `pip install -r requirements-dev.txt` (pytest, pytest-cov, mypy, ruff, black); `pytest` aus dem Repo-Root; Core muss auf dem Pfad liegen (Workspace-Regel: im `lyndrix-core-dev`-Container per `docker cp` + `PYTHONPATH=/app`, bei Cluster-Bench `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local`; `lyndrix-dev/CLAUDE.md`).

## 8. Cluster-Besonderheiten

- Kein Lease, kein `placement`, kein Single-Writer. Docstring: "Routes and bus handlers run on every instance; all state lives in the shared DB" (`entrypoint.py:52-57`).
- Tabellen-Bootstrap unter `db_instance.advisory_lock("lyndrix_bootstrap")` (`getattr`-geschützt), "already exists" (1050/1060/1061) gilt als Erfolg (`app/model/database.py:4-5,89-112`); fehlgeschlagene Init wird bis 5x wiederholt (`service.py:93-105`).
- Sessions READ COMMITTED (`database.py:173-185`); Writes über `retry_write` auf 1020/1205/1213 (`database.py:73`, `service.py:358-375`) plus **ein** Re-Run nach `IntegrityError` ("lost insert race" -> Lookup sieht Gewinner-Zeile und absorbiert dorthin). Das ist der Mechanismus gegen doppelte Identitäten: **UNIQUE(scheme,value)** in der DB (`models.py:73`) + Re-Read; zusätzlich UNIQUE `open_fingerprint` gegen doppelte Konflikte (`service.py:552-557`).
- `last_seen_at` wird höchstens alle 30 s geschrieben (`service.py:42,520-521`), um Hot-Row-Kollisionen zu vermeiden.
- Der im Briefing erwähnte **CorrelationStore-Arbiter existiert in diesem Plugin nicht**; `CorrelationStore` ist ein Core-Baustein (`lyndrix-core/app/core/components/messaging/logic/correlation.py`, exportiert `core/api/__init__.py:277`) für interaktive Aktionen, hier ohne Bezug.
- **Lücke für JSON-Attribute:** `_absorb`/`_update_tx` machen Read-Modify-Write der **ganzen** `attributes`-Spalte (`service.py:508-510,669-672`). Zwei Instanzen, die gleichzeitig verschiedene Schlüssel derselben Entity schreiben, können sich gegenseitig überschreiben (last-write-wins; READ COMMITTED verhindert das nicht, kein Row-Lock, keine Versionsspalte).

## 9. Einhängepunkte für Plan 7.4

Ziel laut Plan: Attribut `maintenance` am CI (aktuelles/nächstes Fenster, Quelle, Override) als Read-Model, gespeist aus `maintenance:windows_published` / `override_started` / `override_ended`, in `GET /entities/{id}` sichtbar (Plan §7.4, §6).

### 9.1 Speicherung: eigene Tabelle statt `attributes`-Schlüssel (Empfehlung)

Fakten gegen die Nutzung von `attributes["maintenance"]`:
1. Kein Namespacing; `identify`/`PATCH` mit `attributes` können den Schlüssel überschreiben (`service.py:508`), ebenso `replace_attributes=True` (`:669`).
2. Whole-column RMW-Race (Abschnitt 8) bei zwei Quellen (Orchestrator-Fenster + Monitoring-Overrides) auf derselben Entity.
3. Jede Änderung löst `entity_updated` mit vollem Detail aus (`service.py:656`).
4. Fenster sind pro **Quelle** zu führen (Plan §6: `source`, `replaces:true`, mehrere Emitter: Orchestrator, Monitoring), Overrides pro `override_id` mit `until` - das ist mehrzeilig.

Vorschlag (neue Datei, additiv): `app/model/models.py` -> Tabelle `registry_maintenance` mit `id`, `entity_uuid` (FK, idx), `kind` (`window`\|`override`), `source` (String 64), `override_id` (nullable, UNIQUE mit `entity_uuid`), `windows` JSON (nur bei `window`), `until` DateTime (nur Override), `reason`, `updated_at`; UNIQUE(`entity_uuid`,`source`,`kind`,`override_id`). `create_all` legt neue Tabellen idempotent an (`database.py:105,110`), kein `_NEW_COLUMNS`-Eintrag nötig (der ist nur für neue Spalten bestehender Tabellen, `database.py:34-37`).
Alternative minimal: JSON-Spalte `maintenance` auf `registry_entities` (neue Spalte per `_NEW_COLUMNS["registry_entities"]`, `database.py:34`), dann weiter RMW-Risiko, nur sinnvoll mit Row-Lock/`SELECT ... FOR UPDATE`.

### 9.2 Bus-Handler
- Datei `entrypoint.py`: drei neue `@ctx.subscribe("maintenance:windows_published" | "maintenance:override_started" | "maintenance:override_ended")` im Stil von `_on_relate` (`entrypoint.py:275-293`): Payload validieren, `svc.is_ready` prüfen, Arbeit per `asyncio.to_thread`, Fehler über `_fail` (`:182-186`).
- `permissions.subscribe` um die drei Topics erweitern (`entrypoint.py:94-103`); sonst werden sie vom Core nicht zugestellt. Optional `emit`: `entity_registry:entity_updated` existiert schon.
- Datei `app/logic/service.py`: neue Methoden `apply_windows(source, items, replaces)`, `start_override(override_id, ci_ids, until, reason, source)`, `end_override(override_id)`, Lese-Helfer `maintenance_for(s, uuids)`; Einbau in `_entity_detail` (`service.py:156-190`, Feld `data["maintenance"]`) und `_summaries`/`list_entities` (`:233-236`, `:314-320`) per **einer** IN-Abfrage (kein N+1; Listen laufen bis limit 1000).
- Zuordnung `ci_id` -> Entity: `_resolve_live(s, uuid)` benutzen (folgt Tombstones, `service.py:145-154`), unbekannte `ci_id` still ignorieren (Plan §2.5 "degradieren"). **Keine** Entity per `identify` anlegen.
- Semantik `replaces:true`: pro `(source)` alle bisherigen `window`-Zeilen löschen und neu schreiben, in **einer** Transaktion über `self._write(...)` (`service.py:358-375`) - Retry/IntegrityError-Handling kommt gratis.
- "Nächstes Fenster": Das Plugin braucht die Zeitauswertung. Plan §5 verspricht eine Core-Bibliothek (API 1.13); bis dahin nur die rohen Schedules speichern und `current/next` beim Lesen berechnen (nicht beim Schreiben, sonst veraltet der Wert ohne Timer). Es gibt im Plugin keinen Scheduler - ein abgelaufenes Override muss ebenfalls beim Lesen über `until < now` ausgeblendet werden, weil `override_ended` verloren gehen kann (fire-and-forget).

### 9.3 Cluster-Sicherheit der Bus-Writes
- Handler laufen nur auf der Instanz, die das Event bekommt (lokales Bus-Delivery, `entrypoint.py:52-53`). Emittiert der Orchestrator (läuft control plane auf jeder Instanz) mit `cluster=True`, bekommen **alle** Registry-Instanzen das Event und schreiben dieselben Zeilen -> muss idempotent sein (UNIQUE-Schlüssel + `delete`+`insert` in einer Transaktion, Retry auf 1020/1205/1213, `IntegrityError` = "Peer war schneller"). Emittiert er lokal, geht das Event verloren, wenn die Registry nur auf anderer Instanz "aktiv" wäre - da das Plugin `placement=any` ist, ist jede Instanz Empfänger ihres lokalen Events; die Zustandsquelle ist die DB, daher reicht lokales Handling. Offene Entscheidung: welche Variante der Emitter nutzt (Plan §6 sagt dazu nichts).
- Kein Lease nötig, solange die Writes idempotent sind.

### 9.4 REST/UI
- `GET /entities/{uuid}` (`api/__init__.py:102-108`) und `GET /entities` (`:88-100`) liefern Feld `maintenance` automatisch, wenn es in `_entity_detail` / Listen-Dict ergänzt wird; Form-Vorschlag: `{"active": bool, "current": {...}|null, "next": {"start":..}|null, "override": {"id","until","reason","source"}|null, "sources": [...]}`.
- Optional Filter `GET /entities?in_maintenance=true` (`service.list_entities`, `:198-236`) und Batch-Lesen für Konsumenten (`GET /maintenance?ci_ids=...`), da heute nur Einzel-GET existiert. Neue Route unter `/maintenance` (nicht `/settings`).
- Optional Bus-Event `entity_registry:entity_updated` mit `changes:["maintenance"]` (Konsumenten kennen das Tag-Schema, `service.py:451,597,656`); bei Massen-Publish besser nicht pro Entity emittieren (Volumen: volles Detail pro Event).
- UI-Badge: `app/ui/react/PluginApp.tsx`:
  - Liste: in der Typ-Zelle neben `StatusBadge` (:207) - neues `MaintenanceBadge` (z. B. `lx-badge--paused`, Icon `build`/`engineering`, Titel = Quelle/Ende).
  - Detail: Kopfzeile neben `StatusBadge` (:285) und neue Karte "Wartung" (aktuelles/nächstes Fenster, Override mit Ende, Quelle) zwischen Attributes/Relations und ITSM-Karte (:367).
  - Typ `Entity` in `lib/api.ts` (:27ff) um `maintenance?` erweitern; Strings in `locales/entity_registry.{de,en}.json` (`check_i18n_keys.py` prüft Parität).
  - Bundle neu bauen (`app/ui/static/ui_bundle.js` ist getrackt).
  - Falls später Panel-Nutzung: `PluginApp` kennt kein `panel`-Prop (`PluginApp.tsx:508`).

### 9.5 Host-CI eines Service-CI
- Heute: Relation `service --runs_on--> host` wäre der saubere Weg (`entity_types.yml:78-83`, `service.py:761-785`), wird aber von **keinem** Plugin gepflegt. Konsument (Monitoring/ITSM) kann sie per `GET /entities/{id}` (`relations[]` mit `direction`, `other_uuid`, `other_entity_type`, `service.py:178-189`) oder `resolve` lesen.
- Es gibt keinen Service-Emitter (Abschnitt 2.2). Damit Plan §4.2 Vererbung Service->Host in der Registry gelingt, braucht es (a) einen Emitter, der Services als `entity_type: "service"` identifiziert und `entity_registry:relate` (`runs_on`) auslöst (Orchestrator-Publish, `engine.py:1192-1260`, ist der natürliche Ort), oder (b) der Orchestrator liefert `host_ci_id` direkt im Event/Inventory (Plan nennt `monitoring:outage_persisted` mit `host_ci_id?`, §6). Empfehlung: (b) als Pflichtweg, (a) zusätzlich, damit die Registry die Wahrheit bleibt.
- Optionales Hilfs-Feld in der Registry: `GET /entities/{uuid}/host` (liefert via `runs_on` den Host) - nur sinnvoll, wenn (a) umgesetzt ist.

### 9.6 Kind-Vokabular vs. Plan
Plan §4.2 (`incident_defaults`) und §6 verwenden `service | host | vm | container`; Registry hat `service, host, hypervisor, container, network, itsm_record, generic` (`entity_types.yml`).
- `vm`: **nicht vorhanden**; VMs/LXC sind `host`. Das Plan-Schema `kind: host|vm|service` (§6) liefert also einen Wert, der kein Registry-Typ ist. Entweder Plan anpassen (`vm` streichen bzw. `hypervisor` ergänzen: physische Nodes sind `hypervisor`, `engine.py:1189`) oder im Katalog einen Typ `vm` ergänzen (aufwendig: Orchestrator müsste `vm` statt `host` identifizieren, Typ-Mismatch wird sonst nur geloggt, `service.py:442-447`; bestehende Entities behalten ihren Typ).
- `hypervisor` fehlt im Plan; `incident_defaults` sollte mindestens `hypervisor` kennen (Plan "Typen = Entity-Registry-Kinds", §9.4).
- Der Katalog ist hot-reloadbar (`POST /catalog/reload`), aber es gibt **keinen** Mechanismus für andere Plugins, das Vokabular zu erweitern; nur Repo-Änderung an `catalog/entity_types.yml`. Zum Lesen: `GET /catalog` (`api/__init__.py:260-262`) - das Monitoring kann die gültigen Typen für `incident_defaults` daraus validieren.

## 10. Abweichungen vom Plan / offene Fragen

1. **`vm` ist kein Registry-Kind** (s. 9.6). Entscheidung nötig: Plan auf `host|hypervisor|service|container` normieren (empfohlen, ohne Registry-Änderung) oder Katalog erweitern.
2. **Kein Service-Emitter, keine Relationen im Betrieb** (s. 2.5, 9.5). Plan §4.2 "Vererbung Service -> Host" ist über die Registry heute nicht abbildbar; `host_ci_id` muss vom Emitter kommen oder der Orchestrator muss `service`-Entities + `runs_on` anlegen.
3. **`maintenance` als `attributes`-Schlüssel ist unsicher** (kein Namespace, RMW-Race, Event-Rauschen, s. 9.1). Plan sagt "Attribut"; empfohlen wird eine eigene Tabelle, nach außen weiter als Feld `maintenance` in `GET /entities/{id}`.
4. **Plan §6 "Registry" als Konsument von `windows_published`**: items tragen `kind: host|vm|service`; die Registry sollte `kind` ignorieren und nur `ci_id` verwenden (Typ ist dort bereits gespeichert), um den `vm`-Konflikt zu umgehen.
5. **Lese-Zeit-Auswertung** (current/next, abgelaufene Overrides) statt Schreib-Zeit, weil das Plugin keinen Timer hat und Events verloren gehen können (s. 9.2). Abhängig vom Core-Zeitmodell (Plan §5, API 1.13).
6. **Cluster-Zustellung der `maintenance:*`-Events** (lokal vs. `cluster=True`) ist im Plan nicht festgelegt (s. 9.3).
7. **Event-Rauschen:** soll die Registry bei Maintenance-Änderungen `entity_updated` (mit vollem Detail) emittieren? Empfehlung: kein Pro-Entity-Event beim Massen-Publish; höchstens ein Sammel-Event oder gar keins.
8. **Berechtigung/Provenienz:** Events auf dem Bus sind ungeprüft; jedes Plugin mit `emit`-Permission auf `maintenance:*` kann Fenster setzen. Die Registry sollte `source` pro Zeile speichern und nicht vom Payload abhängige Löschungen auf fremde Quellen zulassen (`replaces` nur innerhalb derselben `source`).
9. **Retired/merged Ziel-Entities:** `_resolve_live` folgt Tombstones, `status=retired` bleibt auflösbar (`service.py:145-154`) - Entscheidung, ob Fenster an retired CIs gespeichert werden (Vorschlag: ja, aber in Listen nicht anzeigen).
10. **Doku:** `documentation.md` (Deutsch) ist bei Erweiterung um Tabelle, Route und Events zu ergänzen (Tabellen-Übersicht `documentation.md:31`, Bus-Tabelle :110ff); `CHANGELOG.md` + Version/Tag-Bump nach Workspace-Konvention (nächste Minor, Manifest + Tag, `entrypoint.py:75`).
11. **Tests:** neue Tests im Stil der `svc`-Fixture (`tests/test_registry.py:25-55`) für `apply_windows` (replaces je Quelle), Override Start/Ende/Ablauf, unbekannte `ci_id`, Tombstone-Auflösung, Idempotenz bei doppelter Zustellung; Bus-Handler bisher ungetestet - mit `_FakeCtx` ergänzen.
