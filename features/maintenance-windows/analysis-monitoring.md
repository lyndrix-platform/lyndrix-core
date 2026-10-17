# Analyse Monitoring-Plugin für Wartungsfenster (Plan Abschnitt 7.2)

Stand 2026-10-17, read-only P0-Sweep (Sonnet). Der Sweep lieferte den Text als Bericht zurück; diese
Datei ist die unveränderte Ablage. Pfade relativ zu `plugins/lyndrix-plugin-monitoring`, sofern nicht
anders angegeben. Das Backend liegt unter `app/controller/`, nicht `app/logic/`. Die Umbenennung ist
als TODO vermerkt (`entrypoint.py:25-28`).

## 1. Überblick

| Punkt | Wert | Beleg |
|---|---|---|
| Manifest-ID | `lyndrix.plugin.state_monitoring` | `entrypoint.py:47` |
| Version | 1.6.2 | `entrypoint.py:49`, `CHANGELOG.md:8` |
| `min_core_version` | 1.6.0 | `entrypoint.py:54-55` |
| `placement` | nicht gesetzt (Default `any`); Prober-Exklusivität läuft über Lease-Code im Service | `entrypoint.py:46-167` |
| `auto_enable_on_install` | False | `entrypoint.py:56` |
| UI | `react_ui=True`, Route `/monitoring`, zusätzlich NiceGUI-Seite | `entrypoint.py:60-68`, `:369-376` |
| subscribe | `db:connected`, `vault:ready_for_data`, `monitoring:config_upsert`, `monitoring:passive_result`, `monitoring:admin_override`, `monitoring:monitors_changed`, `monitoring:quiesce`, `plugin:settings_changed`, `cluster:changed` | `entrypoint.py:112-126` |
| emit | `monitoring:state_changed`, `monitoring:monitors_changed`, `monitoring:quiesce` | `entrypoint.py:127` |
| Rollen | viewer (api:read), operator und admin (read+write); admin auto-map `INT_ADMIN` | `entrypoint.py:138-166` |
| Core-API | `__api_version__ = "1.12.0"` | `lyndrix-core/app/core/api/__init__.py:149` |

Wichtige Ist-Befunde:

- **Kein `ci_id` im Plugin.** Es gibt keine Spalte, kein Feld und keinen `entity_registry:*`-Aufruf.
  `ctx.request` wird nicht benutzt.
- **Keine Notification im Plugin.** Es gibt kein `notification_service`, kein `messaging:outbound`
  und kein `system:notify`.
  - Einzige Seitenwirkung eines Zustandswechsels ist `monitoring:state_changed`
    (`app/controller/service.py:1467`).
  - Im Workspace gibt es keinen Konsumenten dafür. Nur `lyndrix-core/app/core/bus.py:27,48` fasst es
    fürs Log zusammen; `events_api.py` und `bridge.py` enthalten es nicht.
  - `documentation.md:185-215` (`monitoring:status_changed`, `system:notify`,
    Bus-`monitoring:inventory_sync`) ist veraltet.
- **Inventar kommt per HTTP-POST des Orchestrators, nicht über den Bus.** Details in 4.

## 2. Datenmodell

Alle Tabellen verwenden `extend_existing`, haben keine ForeignKeys und keine `relationship()`.

| Tabelle | Spalten | Beleg |
|---|---|---|
| `monitoring_monitors` | PK `monitor_id` String(191); `name`; `monitor_type` (server/service/http/ping); `owner_source` (Default `ui_admin`); `target`; `address`; `host_name`; `service_name`; `logical_group`; `interval_seconds`; `timeout_seconds`; `enabled`; `source_hash`; `config_hash`; `metadata_json`; `latest_state` String(16); `latest_latency_ms`; `latest_error`; `last_checked_at`; `is_admin_overridden`; `uptime_24h/7d/30d`; `created_at`; `updated_at` | `app/model/models.py:26-56` |
| `monitoring_heartbeats` | `id`, `monitor_id` (idx), `state`, `latency_ms`, `error_message`, `source`, `created_at` (idx); Composite-Index `(monitor_id, created_at)` aus `_ensure_schema` | `models.py:59-69`, `service.py:726-744` |
| `monitoring_daily_aggregates` | `monitor_id`, `day`, `total/up/down/paused_samples`, `avg_latency_ms`, `latency_samples`; unique `(monitor_id, day)` | `models.py:72-93` |

- Weitere Tabellen gibt es nicht. Die Uptime-Fenster sind Spalten auf dem Monitor, stündlich neu
  berechnet (`service.py:1184-1226`).
- **Zustände:** `UP`, `DOWN`, `PAUSED`, `UNKNOWN` (`models.py:12-16`), kein `MAINTENANCE`.
  - `PAUSED` kommt nur von Admin-Pause oder `enabled=False` (`service.py:1670-1671`, `:1713-1717`).
  - `aggregate_state` (DOWN > UNKNOWN > UP > PAUSED) steht in `app/ui/styles.py:78-90`. Ein
    unbekannter Zustand fällt dort auf `UNKNOWN`.
- **Identität:** `monitor_id` folgt der Konvention `host:<host_name>` bzw. `service:<host>:<service>`
  (`service.py:2164`, `:2195`; `_host_key_for` `:2060-2067`).
- **Host, Service, Container:**
  - Host = `server`, ICMP mit TCP-Fallback (`:601-612`).
  - Service = `http` oder `service` (`:2192`).
  - Container-Probe nur bei `metadata.deploy_type == "docker_compose"` plus `address` und
    `service_name` (`:614-616`).
  - Die Beziehung Service→Host läuft nur über `host_name` und das ID-Präfix.
- **Tags/Gruppen:** keine Tag-Tabelle. Es gibt `logical_group` (Site/Stage, `:2171`, `:2203`) und
  `metadata_json.groups` (`:2175`, `:2209`).
- **Besitz:** `owner_source` `orchestrator_service` oder `iac_orchestrator` gilt als „managed"
  (`:1850`). Upsert eines fremd besessenen Monitors wirft `ValueError` (409), außer mit `force`
  (`:1649-1650`).

## 3. Probe-Loop und Transition-/Notification-Pfad

Ablauf, nur auf der Prober-Instanz:

1. `setup()` ruft `queue_bootstrap()` und `init_placement()` (`entrypoint.py:311-313`).
   `init_placement` lädt die Settings und ruft dann `claim_prober_lease()` (`service.py:401-405`).
2. Lease erhalten → `start_probing()` (`:231-243`) → `start()` (Housekeeping-Jobs, `:184-200`) und
   `queue_bootstrap()`.
   - `_bootstrap_async` (`:833-850`) legt die Tabellen unter Advisory-Lock `lyndrix_bootstrap` an
     (`:715-724`), synct die Scheduler-Jobs, macht `refresh_rollups` und startet den Sweep.
3. Pro Monitor gibt es einen Interval-Job `monitor:<id>` (`scheduler.py:58-87`, Anlage in
   `_sync_job_on_loop` `service.py:1534-1569`). PAUSED oder disabled bedeutet: kein Job (`:1548-1556`).
4. `run_scheduled_probe` (`:2069-2149`):
   - Skip bei in-flight, quiesced oder nicht owned (`:2081`).
   - Phase 1: `_load_probe_record` (`:566-590`).
   - Circuit-Breaker pro Host, Schwelle 8 (`:2095-2098`, `:114`).
   - Phase 2: `_dispatch_probe` (`:592-626`).
   - Flap-Dämpfung: DOWN wird erst nach 3 Fehlschlägen geschrieben, und nur wenn der alte Zustand
     UP/None war (`:2128-2134`).
   - Phase 3: `_save_probe_result` im Thread (`:2138`). Fehler 1020/1205/1213 werden verworfen
     (`:2139-2147`).
5. `_save_probe_result` (`:628-646`) nutzt `_rc_session()` (READ COMMITTED, `:123-135`), ruft
   `_store_heartbeat` und committet.
6. **`_store_heartbeat`** (`:1478-1506`):
   - setzt `latest_state/latency/error/last_checked_at/updated_at`
   - legt den Heartbeat an
   - ruft `_bump_daily_aggregate` (`:1073-1110`, MySQL-Upsert `:1120`)
   - ruft `_emit_transition`
7. **`_emit_transition`** (`:1462-1476`):
   - kein Emit, wenn sich der Zustand nicht ändert
   - sonst `_view_cache.clear()` und `self._emit("monitoring:state_changed", {monitor_id,
     previous_state, new_state, timestamp, error_message})`
   - der Emit passiert vor dem Commit

Weitere Aufrufer von `_store_heartbeat` bzw. `_emit_transition` laufen auf jeder Instanz, auch auf
Standby:

- `ingest_passive_result` (Bus `monitoring:passive_result` und `POST /passive`;
  `entrypoint.py:355-360`, `service.py:1741-1757`)
- `apply_admin_override(pause)` (`:1718`)

Notification: nirgends im Plugin (siehe 1).

## 4. Events und Inventory-Sync

| Richtung | Topic | Payload | Beleg |
|---|---|---|---|
| sub | `db:connected` | `queue_bootstrap` | `entrypoint.py:323` |
| sub | `vault:ready_for_data` | `reload_settings` | `entrypoint.py:335` |
| sub | `plugin:settings_changed` | `{plugin_id}` → `reload_settings` bei eigener ID | `entrypoint.py:343-346` |
| sub | `cluster:changed` | Resync, nur im Shard-Modus | `entrypoint.py:339`, `service.py:395` |
| sub/emit | `monitoring:monitors_changed` | `{reason, count, instance_id}`; `reason` ∈ upsert, override, inventory, maintenance, probe_all; Peers sehen `_origin` | `service.py:474-493`, `:436-446` |
| sub/emit | `monitoring:quiesce` | `{on, ttl}` mit `cluster=True` | `service.py:499-541` |
| sub | `monitoring:config_upsert` | `MonitorUpsert` | `entrypoint.py:348` |
| sub | `monitoring:passive_result` | `PassiveResult` | `entrypoint.py:355`, `models.py:118` |
| sub | `monitoring:admin_override` | `AdminOverride` | `entrypoint.py:362`, `models.py:127` |
| emit | `monitoring:state_changed` | siehe 3 | `service.py:1467` |

**Inventory-Sync (Ist):**

- Es gibt kein Abo auf `iac:inventory_updated`.
- Der Orchestrator baut das Payload in
  `lyndrix-plugin-iac-orchestrator/app/controller/engine.py:1016-1089`. Es hat
  `owner_source="orchestrator_service"`, `source_revision` (SHA256), `hosts[]` und `services[]`.
- Er POSTet es mit `LYNDRIX_SYSTEM_API_KEY` an
  `/api/plugins/lyndrix.plugin.state_monitoring/inventory-sync` (`engine.py:1101-1134`). Ausgelöst
  wird das in `NativeGenerateStage` (`:149`) und beim Boot (`iac-orchestrator/entrypoint.py:305`).
- Empfang: `POST /inventory-sync` → `ingest_inventory` → `ingest_inventory_snapshot`
  (`service.py:1024-1033`, `:2155-2265`).
  - Pro Host entsteht `host:<name>` (SERVER).
  - Pro Service entsteht `service:<host>:<name>` (HTTP bei URL-Ziel, sonst SERVICE).
  - Verschwundene Monitore dieser Quelle werden samt Heartbeats und Aggregaten gelöscht (`:2222-2263`).
  - Danach folgt `apply_monitor_set_change("inventory")` (`:552`).
- **`ci_id` kommt im Monitoring-Payload nicht an.**
  - Nur das separate Event `iac:inventory_updated` (`{hosts:[…ci_id]}`, `engine.py:1271-1291`)
    trägt `ci_id`, und nur für Hosts (`_attach_ci_ids` `:1196-1241`; Identifier `iac.host`, `pve.vm`,
    `fqdn`).
  - Services haben nirgends eine `ci_id`.
  - Das Monitoring nutzt dieses Event nicht.
- `queue_inventory_sync` und `_process_inventory_sync_queue` (`service.py:1016-1048`) haben keinen
  Aufrufer (toter Code).

## 5. API-Routen

Mount unter `/api/plugins/lyndrix.plugin.state_monitoring/` auf jeder Instanz. Datei:
`app/controller/api.py`. R = `plugin:…:api:read`, W = `…:api:write` (`api.py:14-15`).

| Methode | Pfad | Perm | Zweck | Zeile |
|---|---|---|---|---|
| GET | `/dashboard` | R | `{monitors, stats}`, gecached | `api.py:34`, `service.py:1411` |
| GET | `/overview` (`group_by`, `hours` 1..168, `include_paused`, `include_unknown`) | R | gruppierte und flache Sicht, gecached | `api.py:38-66`, `service.py:1385` |
| GET | `/monitors/{id}` | R | ein Monitor | `api.py:68` |
| GET | `/history/{id}` | R | 24 h Heartbeats | `api.py:78` |
| POST | `/monitors` | W | Upsert | `api.py:85` |
| POST | `/passive` | W | Passiv-Ergebnis | `api.py:97` |
| POST | `/admin-override` | W | pause/resume/update | `api.py:107` |
| POST | `/inventory-sync` | W | Inventar-Import | `api.py:117` |
| POST | `/clear-states` | W | alles zurücksetzen | `api.py:125` |
| POST | `/maintenance/prune`, `/rebuild-stats`, `/reset-stats`, `/probe-all`, `/delete-monitors/{scope}` | W | Betriebs-Wartung (Settings-Action-Buttons) | `api.py:149-235` |

- Der Namensraum `/maintenance/*` gehört der Plugin-Betriebswartung, nicht den Wartungsfenstern.
  Neue Routen sollten `/windows` und `/overrides` heißen. `/settings` ist reserviert.
- Cache: `_cached()` mit TTL `overview_cache_seconds` (Default 10), Single-Flight, maximal 64 Keys
  (`service.py:1421-1456`). Eine Transition leert ihn nur lokal.

## 6. UI-Komponenten

**React** (`src/ui/PluginApp.tsx`, 1239 Zeilen; `src/ui/lib/api.ts:96-105` `pluginApi`; Poll alle
5 s mit Backoff, `:1106-1175`):

- Typ `MonitorState` `:12`. Datenformen: `ServiceView :14`, `Host :27`, `Group :39`, `Row :52`,
  `Stats :69`. Es gibt kein `ci_id` und kein Fenster-Feld.
- `STATE_COLOR` (`var(--lx-state-up|down|paused|unknown)`) `:140-149`. `STATE_LABEL` und
  `StateBadge` `:320-353`; i18n-Key `state.*` in `locales/monitoring.de.json:32-37`.
- Komponenten: `ServicesInline :375`, `HostHeader :417`, `HostCard :489`, `HostCardDetail :519`,
  `GroupHeader :562`, `CardsView :616`, `TableView :646`, `SplitView :771` (die einzige
  Detailansicht, auf Host-Ebene), `Toolbar :873`, `HeaderCard :1051` (Zähler `:1053-1059`).
- Es gibt keinen Dialog, kein Formular und keine Monitor-Verwaltung im React-UI. Monitor-CRUD
  existiert nur in NiceGUI (`app/ui/settings.py:11`, `:100-140`).
- Farbtokens:
  - `lyndrix-ui/src/index.css:58-61`
  - Whitelist `lyndrix-core/app/core/theming/schema.py:42`
  - `theming/engine.py:275,312`
  - Badge-Klassen `lyndrix-ui/src/index.css:453,460` und `lyndrix-core/app/ui/theme.py:148-149,608`
- NiceGUI-Spiegel: `app/ui/page.py:15`, `toolbar.py:39`, `views/{cards:21,table:49,split:29}.py`,
  `_components/host_card.py:110,129`, `group_header.py:13`, `timeline.py`, `widget.py`
  (Gesamtstatus `:25`), `styles.py:8-90`.
- `.claude/skills/run-state-monitoring/SKILL.md` behauptet, es gebe keine React-Route. Das
  widerspricht `entrypoint.py:60-68`.

## 7. Teststand

- Dateien: `tests/test_service.py`, `test_perf_and_status.py`, `test_cluster_lease.py`,
  `test_cluster_readiness.py`, `test_maintenance.py`, `test_sharding.py` (ca. 50 Tests).
- Fixtures:
  - `conftest.py:12` importiert `core.services` vor `core.api`. Der Meta-Path-Finder
    `_SharedAppFinder` (`:34-62`) macht `monitoring_plugin.app.*` identisch mit `app.*`.
  - Fixture `svc` in `test_perf_and_status.py:51-62`: In-Memory-SQLite mit `StaticPool`,
    `db_instance` per monkeypatch, `create_all`, `ensure_tables()`.
  - `_FakeCtx` (`:22-45`) kennt nur `emit(topic, payload)`, `set_status`, `maintenance`, `log`.
  - Helfer `_monitor` (`:65`) und `_beat` (`:73`, backdatiert Heartbeats).
  - `test_sharding.py` hat einen `_ShardCtx` mit `on_lease`, `my_shard`, `is_worker`,
    `get_all_settings`, `roles`, `instance_id`.
- Lauf: `docker cp <plugin> lyndrix-core-dev:/tmp/t-mon` und dann
  `docker exec -w /tmp/t-mon -e PYTHONPATH=/app:/tmp/pytest-sm -e LYNDRIX_CLUSTER_MODE=single
  -e LYNDRIX_COORDINATOR=local lyndrix-core-dev python -m pytest -q tests`.
  - pytest muss vorher nach `/tmp/pytest-sm` installiert werden. Es geht bei Container-Recreate
    verloren (Docstring `test_perf_and_status.py:1-7`).
- Der Docstring nennt „FKs on", im Code gibt es aber kein `PRAGMA foreign_keys=ON`. Das ist
  irrelevant, solange das Plugin FK-frei bleibt.
- Nicht abgedeckt: `ctx.request`, `ctx.get_setting`, Zeit/Zeitzonen. Die Transition-Logik hat nur
  `test_transition_emit_from_worker_thread_lands_on_loop` (`test_cluster_readiness.py:85`).

## 8. Cluster-Besonderheiten

- **Lease:** `ctx.on_lease("monitoring:prober", acquired=start_probing, lost=stop_probing)`
  (`service.py:310`). Ohne `on_lease` (Core < 1.7) wird direkt geprobt (`:287-290`).
  - `probe_roles` filtert die Bewerber (`:265-308`). Verkleinern gibt die Lease per `release_lease`
    ab (`:312-334`).
- **Standby:** kein Scheduler, kein Sweep, kein Housekeeping. Nur Tabellen (`:837-840`); die API
  läuft weiter. `health()` meldet Standby als gesund (`entrypoint.py:228-248`).
- **Shard-Modus** (`probe_nodes > 1`, Core ≥ 1.8):
  - Keine Lease. Jede berechtigte Instanz probt ihren Anteil via `ctx.my_shard` (`service.py:357-385`).
  - Globales Housekeeping läuft nur auf dem `is_worker("housekeeping")`-Worker (`:202-221`).
  - Der Moduswechsel gilt erst nach Reload (`:419-426`).
  - Folge: Mehrere Prober sind gleichzeitig aktiv.
- **Status:** `_set_status("starting", …, blocking, retry_after=15)` im Start-Sweep, danach `ready`
  (`:933-969`). `maintenance_window()` (`:971`) umschließt Inventar-Import, clear-states, rebuild,
  reset und delete (`api.py:132,162,173,218`).
- **Broadcast:** `_emit_cluster` mit `cluster=True`, Peers sehen `_origin` (`:480-493`).
- **DB-Gotchas bereits umgesetzt:**
  - `_rc_session` (READ COMMITTED) `:123`
  - `_retrying` auf 1020/1205/1213 (`:1857`, `:1865`)
  - Batches 20000/200/500 (`:44`, `:1851-1852`)
  - Quiesce `:524-541`
  - Advisory-Lock für DDL `:715`
  - MySQL-Upsert `:1120`
  - Heartbeat-Write verwirft 1020/1205/1213 `:2139`
- DDL-Hook: `create_all` in `ensure_tables` (`:708-713`) plus `ALTER … IF NOT EXISTS` in
  `_ensure_schema` (`:726-744`).

## 9. Einhängepunkte für Abschnitt 7.2

### 9.1 Tabellen `monitoring_maintenance` und `monitoring_overrides`

- **Datei:** `app/model/models.py`, neben `:59-93`, `extend_existing`, FK-frei.
  - `monitoring_maintenance`: `id, ci_id (idx, null), monitor_id (idx, null), source, schedule_json,
    note, enabled, created_at, updated_at`.
  - `monitoring_overrides`: `override_id PK, ci_ids_json, until, reason, source, created_at, ended_at`.
  - Jede Zeile braucht `ci_id` oder `monitor_id`, solange 9.2 offen ist.
- **DDL:** automatisch über `create_all` (`service.py:708`), bereits unter Advisory-Lock. Eine neue
  Spalte am Monitor braucht einen Eintrag in `_ensure_schema` (`:731`).
- **Logik:** neues Modul `app/controller/maintenance.py` statt noch mehr in `service.py` (2265 Zeilen).
  - `core.api.maintenance` gibt es erst ab API 1.13. Bis dahin braucht es einen `getattr`-Guard oder
    eine lokale Mini-Auswertung; das Risiko ist doppelte Logik.
- **Risiken:**
  - Zeitzonen. Das Plugin arbeitet in UTC, Heartbeats sind naive UTC (`models.py:251`,
    `service.py:1195,1787`).
  - Kein DB-Lesen pro Probe im Hot-Path. Stattdessen ein In-Memory-Cache der aktiven Fenster je
    Instanz, invalidiert per Bus/Cluster-Broadcast.

### 9.2 `ci_id` am Monitor (Voraussetzung)

- **Spalte** `ci_id String(64) null, index` anlegen in:
  - `MonitorRecord` (`models.py:26-56`)
  - `MonitorUpsert` (`:100`)
  - `_serialize_monitor` (`:162`)
  - `upsert_monitor` (`service.py:1655-1692`)
  - `_ensure_schema`
- **Falle:** `config_hash` (`:1653-1654`) muss `ci_id` einschließen, sonst wird sie nie übernommen.
- **Quellen:**
  - Variante 1: Orchestrator ergänzt `ci_id` in `_build_monitoring_inventory_payload`
    (`engine.py:1039-1089`). Dazu müssen die Schritte in `NativeGenerateStage` (`engine.py:149-150`)
    umgeordnet werden, weil `_attach_ci_ids` heute erst in `emit_iac_inventory_updated` läuft.
  - Variante 2: Monitoring abonniert `iac:inventory_updated` (Eintrag in `permissions.subscribe`)
    und matcht über den Host-Namen auf `host:<name>`.
  - Variante 3: Monitoring ruft selbst `ctx.request("entity_registry:identify"/"lookup")`, mit
    Degradierung bei `reply is None`.
- Services haben keine `ci_id`. Die einzige Brücke ist `host_name`: Eine Host-`ci_id` wirkt auf
  `host:<n>` und alle `service:<n>:*`.

### 9.3 Konsum von `maintenance:windows_published` / `override_started` / `override_ended`

- **Datei:** `entrypoint.py`. Drei dünne `@ctx.subscribe`-Handler im Stil von `:327-367`, die per
  `to_thread` an den Service delegieren. Die Topics gehören in `permissions.subscribe` (`:112-126`).
- **Persistenz** in der DB ist Pflicht, weil Standby und Prober dieselben Daten sehen müssen. Nach dem
  Schreiben folgt ein Broadcast über `_emit_cluster`.
  - Der Zweig in `on_monitors_changed` (`service.py:436-446`) macht heute nur `request_resync`. Für
    Fensteränderungen braucht er einen eigenen Pfad „Cache neu laden", z. B.
    `reason="maintenance_windows"`.
- `windows_published` mit `replaces: true` bedeutet: alle Zeilen dieser `source` in einer Transaktion
  ersetzen, mit `_rc_session` und `_retrying`.
- `permissions.emit` (`entrypoint.py:127`) fehlt für `maintenance:windows_published` (manuelle Fenster).
- **Risiko:** Bei `cluster=True`-Emits laufen die Handler auf jeder Instanz, also Doppel-Writes. Ein
  Unique-Key plus Behandlung von IntegrityError als „Peer war schneller" ist nötig.

### 9.4 Unterdrückung zum Transition-Zeitpunkt (State `maintenance`, keine Notification)

- **Zentraler Punkt:** `_store_heartbeat` (`service.py:1478-1506`). Alle Schreibpfade laufen
  hindurch: aktive Probe `:643`, Passiv `:1749`. Vor `record.latest_state = …` kommt ein
  `effective = self._effective_state(record, state, now)`. Ist ein Fenster oder Override aktiv und der
  Zustand DOWN, wird `MAINTENANCE` gespeichert. `error_message` bleibt zur Diagnose erhalten.
- **Enum** `MonitorState` (`models.py:12`) um `MAINTENANCE` erweitern. Folgen:
  - `_bump_daily_aggregate` (`service.py:1098-1110`, `:1120-1156`) kennt keine Wartungsspalte.
    Entweder `maintenance_samples` ergänzen (Migration) oder in `paused_samples` einrechnen
    (vermischt die Semantik).
  - Uptime: `_uptime_from_aggregates` nutzt `total - paused` (`models.py:222-229`). Weitere Stellen,
    die nur PAUSED ausschließen: `_refresh_uptime_windows_sync` (`service.py:1202`) und
    `_calculate_uptime_percentages` (`models.py:201`). `_timeline_uptime_percent` (`:282`) zählt nur UP.
  - `get_timelines` und `aggregate_state` (`styles.py:78-90`): unbekannte Zustände werden zu
    UNKNOWN, `MAINTENANCE` muss dort einsortiert werden.
  - `status_counts` und `stats` (`service.py:1232-1284`): neues Feld `maintenance_count`.
    `health()` optional.
  - `_sync_job_on_loop` (`:1550`) entfernt nur bei PAUSED den Job. Das Probing läuft in Wartung also
    weiter, gut für die Recovery-Erkennung.
  - `_load_probe_candidates` (`:894-903`): `MAINTENANCE` wäre nur „stale"; bei Bedarf aufnehmen.
  - `PassiveResult.state` (`models.py:121`) würde `MAINTENANCE` von außen akzeptieren; ggf. verbieten.
- **Flap-Dämpfung:** Der Wartungs-Check darf erst in `_store_heartbeat` stattfinden, nicht davor.
  Sonst entsteht UP→MAINTENANCE ohne Probe-Beweis.
- **Transition-Event:** Es wird nicht verschwiegen (Cache und Konsumenten brauchen es), sondern
  bekommt `maintenance: true` und `ci_id`. Der Emit sitzt vor dem Commit (`:1506` vs `:644`), das ist
  ein bestehendes Verhalten und liefert bei Rollback ein Phantom-Event. Für die Outage-Logik daher
  nicht verwenden.
- **Notification:** Heute nicht vorhanden. Entsteht später ein Alarmpfad, gehört `in_maintenance`
  zwingend davor.
- Offene Entscheidung: Bleibt ein erfolgreicher Probe im Fenster `UP`? Empfehlung ja, nur DOWN wird
  zu MAINTENANCE.

### 9.5 Manuelle Fenster in der eigenen UI

- **API:** neue W-gated Routen in `api.py` nach `:123`: `GET/POST/DELETE /windows`,
  `POST /overrides`, `DELETE /overrides/{id}`. Danach `_view_cache.clear()` lokal plus
  Cluster-Broadcast.
- **React:**
  - Es gibt keinen Dialog im Code. Neue Dateien unter `src/ui/` mit Dialog „Wartungsfenster" und
    Overrides-Liste.
  - Einhängen über Buttons in `HostCardDetail :519`, `SplitView :771` und `TableView :646`.
  - Die Overrides-Liste kommt als Abschnitt neben `HeaderCard :1051` bzw. ins Toolbar-Menü `:873`.
  - Die Typen `Row`, `ServiceView` und `Host` (`:14-66`) bekommen `maintenance_until?`. i18n-Keys in
    `locales/monitoring.{de,en}.json`.
- **NiceGUI:** Spiegel in `app/ui/settings.py` und `_components/host_card.py`, nur über die neuen
  Routen (API-first).
- `settings_schema` (`entrypoint.py:71-110`) taugt für Schalter und Puffer, nicht für Fensterlisten.

### 9.6 Dashboard-Badge „Wartung bis hh:mm"

- **Backend:** `maintenance_until` in `list_monitors` und `_serialize_monitor` (`service.py:1286`,
  `models.py:162`), berechnet aus dem In-Memory-Cache.
  - `build_grouped_overview` und `flatten_monitors` (`helpers.py:147`, `:271-307`) reichen Felder per
    Whitelist-Dict durch (Zeilen `:290-306`, Host/Service-Views `:180-200`). Dort muss
    `maintenance_until` ergänzt werden.
  - Der Overview-Cache (TTL 10 s) verzögert die Sichtbarkeit um bis zu 10 s; eine Transition leert ihn.
- **React:** `StateBadge` (`:327-353`) bekommt den Zustand `MAINTENANCE` mit optionaler Zeit. Ändern
  müssen sich außerdem `STATE_COLOR` und `STATE_LABEL` (`:140-149`, `:320-325`) sowie die
  `HeaderCard`-Zähler (`:1053-1059`).
- **Farbe:** Ein neues Token `--lx-state-maintenance` ist eine core-übergreifende Änderung, weil die
  Core-Whitelist es nicht kennt. Betroffen: `lyndrix-ui/src/index.css:58-61`, `schema.py:42`,
  `engine.py:275/312`, `theme.py:148-149,608`. Übergangsweise Fallback auf `--lx-state-paused` oder
  `--lx-warning`.
- NiceGUI: `styles.py:8-27`, `widget.py:25`.

### 9.7 Outage-Regel (offline ≥ Y min außerhalb eines Fensters) und Emits

- **„Down since" gibt es heute nirgends.**
  - `updated_at` und `last_checked_at` werden bei jedem Heartbeat überschrieben (`service.py:1493-1494`).
  - Heartbeats werden nach 7 Tagen gelöscht (`:1003`, `:1782`).
  - Flap-Dämpfung und Circuit-Breaker erzeugen Lücken (`:2095-2098`, `:2128-2134`).
  - Rekonstruktion aus Heartbeats ist daher unzuverlässig.
- **Neue Tabelle** `monitoring_outages(monitor_id PK, ci_id, down_since, state
  open|persisted|recovered, persisted_at, rule_json, updated_at)`.
  - `down_since` wird in `_store_heartbeat` beim Wechsel nach DOWN gesetzt, im selben Commit wie der
    Heartbeat (konsistent, kein Phantom wie beim Emit).
  - Geschlossen wird bei der Rückkehr, ebenfalls dort und in `apply_admin_override` (`:1703`).
  - `delete_monitors` und `clear_states_db` (`:1929`, `:1803`) müssen die Tabelle mitleeren.
- **Auswertung:** periodischer Interval-Job (z. B. 60 s, `id="monitoring:outages"`) in `start()`
  (`:184-200`), läuft nur auf dem Prober.
  - Im Shard-Modus nur für `owns(monitor_id)` (`:383`).
  - Einmalige Zustellung über bedingtes `UPDATE … WHERE state='open'` (Rowcount 1 = Gewinner). Das
    schützt auch beim Lease-Wechsel und bei einem Race zweier Instanzen.
- **Emits:** `monitoring:outage_persisted` `{ci_id, host_ci_id?, service?, since, duration_s, rule}`
  und `monitoring:outage_recovered` `{ci_id, since, recovered_at}` über `self._emit` (`:168`). Beide
  gehören in `permissions.emit` (`entrypoint.py:127`).
  - Ohne `ci_id` wird kein Event gesendet, der Monitor wird übersprungen.
- **Regeln:** lokale Regel pro Monitor oder die Bus-`rule`; globale Defaults je CI-Typ als Settings (9.8).

### 9.8 Laufzeit-Notschalter je CI-Typ

- **Datei:** `entrypoint.py` `settings_schema` (`:71-110`). Neue Felder z. B.
  `outage_rules_enabled_host/_vm/_service`, `outage_default_offline_minutes`,
  `override_buffer_minutes` (15), `override_max_hours` (4).
  - Das Schema nutzt bisher nur `int`, `str` und `action`. Ob `kind: bool` existiert, ist im Core zu
    prüfen.
- **Anwendung:** `apply_settings` (`service.py:994-1010`) erweitern. Änderungen kommen über
  `plugin:settings_changed` ohne Reload an. `ctx.get_setting` wird im Plugin nicht benutzt, nur
  `get_all_settings` (`:407`).
- Die CI-Typ-Zuordnung ist ableitbar aus `monitor_type` (`server` = Host, sonst Service). VM vs.
  Host unterscheidet nur `metadata.terraform` (`service.py:2178`) oder der `entity_type` aus der
  Registry.

## 10. Abweichungen vom Plan / offene Fragen

1. **Kein `ci_id` am Monitor.** Der Plan setzt es voraus. Der Orchestrator liefert es dem Monitoring
   nicht (nur Docker-Manager u. a. via `iac:inventory_updated`). Services haben nirgends eine `ci_id`.
   Entscheidung nötig: Variante 1, 2 oder 3 aus 9.2.
2. **Kein Notification-Pfad im Monitoring.** Der Plan („keine Notification Discord/Notification-Router"),
   `documentation.md:198` und `docs/index.md:8` beschreiben etwas, das im Code fehlt.
   `monitoring:state_changed` hat keinen Konsumenten. Alarmierung wäre ein eigenes Feature.
3. **Inventar kommt per HTTP-POST mit System-API-Key** (`engine.py:1101-1134`), nicht über den Bus.
   `maintenance:*` kommt dagegen über den Bus, also zwei Transportmuster im selben Plugin.
4. **Emit/Subscribe-Allowlists** (`entrypoint.py:112-127`) enthalten weder `maintenance:*` noch
   `monitoring:outage_*`.
5. **„Nur die Lease-Instanz wertet aus" gilt nur im Lease-Modus.** Im Shard-Modus proben mehrere
   Instanzen, die Auswertung muss `owns()` respektieren und die Zustellung per DB-Gewinner absichern.
   Passiv-Ergebnisse und Admin-Overrides laufen auf jeder Instanz, die `down_since`-Pflege muss
   deshalb DB-atomar sein.
6. **Semantik „offline ≥ Y min außerhalb eines Fensters" ist unterbestimmt:** kumulativ (Fensterzeit
   abziehen) oder Neustart nach Fensterende? Flap-Dämpfung (3 Fehlschläge, `service.py:113`) und
   Intervall ≥ 10 s (`models.py:110`) verschieben `down_since` um bis zu ca. 3 Intervalle nach hinten.
   Der Circuit-Breaker stoppt Probes ganz (`:2095`).
7. **Aggregat- und Uptime-Semantik für `maintenance`** ist im Plan nicht spezifiziert (Nenner, eigene
   Spalte vs. `paused_samples`, Sortierung in `aggregate_state`). Das Token `--lx-state-maintenance`
   ist eine Core- und UI-Änderung.
8. **`placement`** ist nicht gesetzt. `singleton` wäre falsch, weil die API auf allen Instanzen laufen
   muss.
9. **`documentation.md`** („Events", ab Zeile 185) ist veraltet und sollte vor 7.2 neu geschrieben
   werden (Deutsch).
10. **Testlücken:**
    - `_FakeCtx.emit(topic, payload)` kennt `cluster=` nicht. Der Service fängt das über den
      `TypeError`-Zweig `service.py:485-488` ab, getestet wird der `cluster=True`-Pfad damit nicht.
    - Es fehlen `ctx.request` und eine Uhr-Injektion; `_utc_now` ist modulglobal (`models.py:144`),
      per monkeypatch ersetzbar.
11. **Toter Code:** `queue_inventory_sync` / `_process_inventory_sync_queue` (`service.py:1016-1048`)
    sind keine Vorlage für den Bus-Konsum.
12. **Verzeichnis:** Der Plan nennt `app/logic/`, tatsächlich ist es `app/controller/`. Neue Module
    dort ablegen oder die Umbenennung vorher erledigen, um einen Doppel-Umzug zu vermeiden.
