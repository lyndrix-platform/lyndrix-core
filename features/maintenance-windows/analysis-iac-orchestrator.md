# Analyse-Sweep P0: IaC Orchestrator (`lyndrix-plugin-iac-orchestrator`)

Stand 2026-10-17, read-only (Sonnet), zu Abschnitt 7.1 / 10 des Plans. Der Sweep lieferte den Text
als Bericht zurück; diese Datei ist die unveränderte Ablage. Pfade relativ zu
`plugins/lyndrix-plugin-iac-orchestrator/`.

## 1. Überblick

- Manifest-ID `lyndrix.plugin.iac_orchestrator` (`entrypoint.py:21`). Version `1.5.0`
  (`entrypoint.py:23`). Eine zweite Kopie `PLUGIN_VERSION` steht in `app/api.py:35` und muss synchron
  gehalten werden.
- `min_core_version` ist `1.4.0` (`entrypoint.py:28`). Wird `core.api.maintenance` (API 1.13)
  importiert, entweder `min_core_version` anheben oder per try/except importieren.
- Placement ist `any`. Die Control Plane läuft auf jeder Instanz, die Ausführung nur auf Instanzen
  mit Rolle `runner` (`entrypoint.py:29-34`, `app/controller/service.py:55-65`).
- `permissions.subscribe` (`entrypoint.py:102-103`): `vault:ready_for_data`, `iac:webhook_verified`,
  `git:status_update`, `db:connected`, `socket:response`, `entity_registry:identified`,
  `iac:job_queued`, `iac:job_abort_requested`.
- `permissions.emit` (`entrypoint.py:104-108`): `iac:pipeline_started`, `iac:webhook_verified`,
  `iac:inventory_updated`, `entity_registry:identify`, `server_manager:register_server`, `git:sync`,
  `git:commit_push`, `system:notify`, `user:notify`, `socket:request`, `messaging:outbound`,
  `iac:job_queued`, `iac:job_abort_requested`.
- Die Topics `maintenance:windows_published`, `maintenance:override_started` und
  `maintenance:override_ended` müssen in `permissions.emit` ergänzt werden.
- Rollen `admin`, `operator`, `viewer` und die Custom-Permission `api:infra_apply` stehen in
  `entrypoint.py:49-80`.
- Das Manifest hat kein `settings_schema`, keine Panels und keine Nav-Entries. Es hat
  `ai_instructions` und `ai_instructions_route` (`entrypoint.py:37-43`) sowie
  `settings_ui_route="/iac/settings"` (`entrypoint.py:100`).

## 2. Datenmodell

Modelle in `app/model/models.py`, Zugriff in `app/model/database.py` (`JobDatabase`) und
`app/controller/queue.py` (`JobQueue`).

**`iac_orchestrator_jobs`** (`models.py:11-36`, `IaCJob`)

- `id`: Integer PK.
- `pipeline_type`: String(50), z. B. `single_service:<svc>` (`engine.py:663`, `queue.py:104-111`).
  Bei langen Servicenamen besteht ein Längenrisiko.
- `status`: String(20), Default RUNNING. Gesetzte Werte: QUEUED, RUNNING, SUCCESS, FAILED, ERROR,
  ABORTED (`queue.py:30-33`, `database.py:100`).
- `progress` und `current_step`.
- `start_time` (server_default now) und `end_time`. Bei QUEUED-Jobs ist `start_time` die Enqueue-Zeit.
- `logs` (Legacy) und `pending_tasks`.
- `payload`: Trigger-Payload als JSON (`queue.py:207`).
- `dedupe_key`: String(255) (`queue.py:73-89`).
- `owner_instance`, `claimed_at`, `heartbeat_at`.
- `runners`: Live-Snapshot als JSON.

Weitere Tabellen:

- `iac_orchestrator_job_logs`: eine Zeile je Logzeile, mit `seq`.
- `iac_orchestrator_job_tasks`.
- `iac_orchestrator_state`: Snapshots, `commit_hash`.

Schema-Migration:

- `JobQueue.ensure_schema()` (`queue.py:137-167`) legt Spalten per `ALTER TABLE ADD COLUMN` an
  (Dict `wanted`, `queue.py:148-155`). Das läuft unter `advisory_lock("lyndrix_bootstrap")`, Fehler
  1060 wird toleriert.
- Aufgerufen wird es aus `IaCService.bootstrap_db` (`service.py:209-225`; Boot und `db:connected`,
  `entrypoint.py:274-278`).
- Neue Spalten (`not_before`, `window_ci_ids`, `override_id`) gehören in `wanted` und ins ORM-Modell.
  Die SQLite-Variante wird automatisch ersetzt (`queue.py:156-158`).

`ci_id`-Nutzung:

- `ci_id` gibt es nur für Hosts, nur im Event `iac:inventory_updated` und in
  `server_manager:register_server` (`engine.py:1196-1269`).
- Sie wird nicht in der DB persistiert und hängt nicht an Jobs.
- Services haben im Plugin keine `ci_id`.
- Strong Keys: `iac.host`, `pve.vm` (`node_name/vm_id`) und `fqdn`, letzteres nur wenn der Hostname
  einen Punkt enthält (`engine.py:1212-1218`).

## 3. Events

**Emit**

- `iac:webhook_verified`:
  - Quellen: `controller/api.py:577, 654, 736, 784, 798, 813, 854, 1118` sowie der
    Host-Rollout-Handoff in `engine.py`.
  - Payload: `{pipeline_type, service_name, service_branch, manual, trigger?, ...}`.
  - Verarbeitet wird es lokal in `entrypoint.py:283-287`, dort über `enqueue_from_event`
    (`service.py:122-141`).
- `iac:job_queued` (`service.py:83`, `cluster=True`): Payload `{job_id}`.
- `iac:job_abort_requested` (`api.py:1164-1166`): Payload `{job_ids}`.
- `iac:inventory_updated` (`engine.py:1271-1295`, Emit bei `:1285`):
  - Payload: `{"hosts":[{name, hostname, ip, groups, baseline_roles, terraform (ohne Secrets),
    entity_type host|hypervisor, site, stage, ci_id}]}`.
  - Es gibt kein `services`-Feld und keine Service-`ci_id`.
  - Auslöser: Boot, nur auf Runnern (`entrypoint.py:306`), und `NativeGenerateStage`
    (`engine.py:143-153`), also nach jeder Pipeline mit Generate-Prefix, auch `single_service`.
- `server_manager:register_server` (`engine.py:1245-1269`): je Host mit `ci_id`.
- `entity_registry:identify` per `ctx.request` (`engine.py:1196-1243`, Aufruf bei `:1226`):
  - Items: `{entity_type, identifiers, display_name, attributes, source:"iac-orchestrator"}`.
  - Reply-Topic ist `entity_registry:identified`, Timeout 10 s.
  - Antwort: `{ok, results:[{ok, uuid, conflict, error}]}`.
  - Ohne `ctx.request` oder ohne Antwort bleibt `ci_id=None` (`engine.py:1205-1207, 1228-1231`).
- `git:sync` und `git:commit_push` (`engine.py:1989, 2022`): Request/Response über `request_id` und
  `git:status_update`. Core-Handler: `lyndrix-core/app/core/components/git/logic/git_service.py:319, 384`.
- `messaging:outbound` und `system:notify` (`api.py:69, 312, 1197`) sowie `ctx.notify` in `engine.py`.

Kein Bus-Event ist der Monitoring-Sync:

- `POST http://127.0.0.1:<PORT>/api/plugins/lyndrix.plugin.state_monitoring/inventory-sync` mit
  `LYNDRIX_SYSTEM_API_KEY` (`engine.py:1101-1143`).
- Payload `{owner_source, source_revision, hosts[], services[]}` (`engine.py:1016-1089`).
- Stage und Site werden aus den Inventory-Gruppen `stage_*`/`site_*` abgeleitet.
- Services stehen hier ohne `ci_id`.

**Subscribe**

- `db:connected` (`entrypoint.py:276`).
- `iac:webhook_verified` (`entrypoint.py:283-287`). Kopien mit `_origin` werden ignoriert.
- `iac:job_queued` (`entrypoint.py:290-293`) ruft `worker.wake()`.
- `iac:job_abort_requested` (`entrypoint.py:296-298`).
- `git:status_update` (Subscribe in `engine.py` `__init__`, Handler `engine.py:1961`).
- `vault:ready_for_data`, `socket:response` und `entity_registry:identified` stehen nur in den
  Permissions und werden nicht per `ctx.subscribe` abonniert.

**Webhook-Pfad bis zum Job**

1. Der öffentliche Router `/api/iac` hängt direkt an `main.app` (`entrypoint.py:251-259`), Handler in
   `app/controller/webhook_router.py:23-82`.
2. Validierung: `_require_gitlab_token` (`controller/api.py:94-103`) vergleicht `X-Gitlab-Token` per
   `hmac.compare_digest` mit dem Vault-Secret `gitlab_webhook_token`. Fehlt das Token im Vault, kommt
   500, bei Mismatch 401.
3. Der Renovate-/Prod-Pfad ist nicht der GitLab-Group-Webhook, sondern der Service-CI-Job
   `prod-trigger-orchestrator`:
   - Er ruft `POST /api/iac/deploy/service/{svc}/gitlab` mit Body `{"branch":"main"}` auf
     (`webhook_router.py:45-52`, `do_trigger_service_deployment` in `api.py:626-656`).
   - Der Group-Webhook `/webhook/gitlab` (`api.py:543-585`) hat `pipeline_events=False`
     (`gitlab_webhooks.py:155-159`, bewusst gegen Doppel-Deploys). `_should_trigger_orchestrator`
     (`api.py:252-278`) lässt merge_request, push usw. nie triggern.
4. Identifikation im Payload:
   - Service ist `service_name` (Pfadsegment, lowercase, validiert).
   - Branch ist `service_branch`.
   - Es gibt kein explizites Stage-Feld und keinen Host im Payload.
   - Die Stage folgt implizit aus dem Branch: `SingleServiceStrategy` mappt `dev` oder `*-dev` auf
     `stage_dev`, `test` auf `stage_test`, sonst `service_<name>` (`engine.py:666-672`). Der Host
     ergibt sich aus dem generierten Inventory.
5. Event-Payload (`api.py:647-653`): `{pipeline_type:"single_service", service_name, service_branch,
   manual:True}`. Ein `trigger`-Feld fehlt, daher wird `requested_by` zu „ui" (`service.py:128`).
   - Danach: `iac:webhook_verified`, `enqueue_from_event`, `JobQueue.enqueue`, Zeile QUEUED,
     `iac:job_queued`.
6. Die CI pollt `GET /api/iac/deploy/service/{svc}/gitlab/status?since_epoch=` (`api.py:659-694`):
   - SUCCESS wird zu `success`, FAILED/ERROR/ABORTED zu `failed`, RUNNING/PENDING zu `running`.
   - Alles andere, also auch QUEUED und künftig SCHEDULED, wird zu `"unknown"`.
   - Die Suche läuft über `pipeline_type == "single_service:<svc>"` mit Zeitfenster auf
     `start_time`/`end_time` (`database.py:148-191`).

## 4. API-Routen

Authentifiziert unter `/api/plugins/lyndrix.plugin.iac_orchestrator/` (`app/api.py`). READ ist
`plugin:<id>:api:read`, WRITE ist `plugin:<id>:api:write`.

| Methode | Pfad | Permission | Zeile |
|---|---|---|---|
| GET | /catalog | read | 46 |
| GET | /ai/instructions | read | 50 |
| GET | /jobs?limit= | read | 60 |
| GET | /jobs/{id}/logs | read | 67 |
| GET | /jobs/{id}/runners | read | 77 |
| GET | /deploy/service/{svc}/status | read | 84 |
| GET | /stats | read | 92 |
| GET | /infrastructure/assignments, /infrastructure/terraform-hosts | read | 96, 102 |
| GET | /service/{svc}/history | read | 108 |
| GET | /settings/general, /settings/schema, /settings/values, /settings/credentials, /settings/webhook-token | read | 119-147 |
| GET | /infra/pending-plan | read | 241 |
| POST | /pipeline | write | 150 |
| POST | /abort | write | 157 |
| POST | /settings/general, /settings/values, /settings/credentials, /settings/webhook-token/generate, /settings/webhooks/sync | write | 161-199 |
| DELETE | /settings/credentials/{alias} | write | 182 |
| POST | /maintenance/clear-stats, /maintenance/clear-failed-jobs, /maintenance/sync-repos | write | 201-211 |
| POST | /deploy/service/{svc} (Body {branch}) | write | 212 |
| POST | /deploy/test-host/{host}, /bootstrap/{host} | write | 220, 228 |
| POST | /infra/plan | write | 236 |
| POST | /infra/pending-plan/apply, /infra/apply | write + api:infra_apply | 249, 266 |
| POST | /infra/pending-plan/dismiss | write | 256 |
| POST | /stream/ticket | read | 276 |

Direkt an der App, Auth im Handler:

- `GET /api/plugins/<id>/stream/jobs` (SSE, `?ticket=` oder `?token=`, `api.py:293`).
- `GET .../jobs/{id}/logs/raw` (`api.py:301`).

Öffentlich unter `/api/iac` mit `X-Gitlab-Token` (`webhook_router.py:33-80`):

- GET /health.
- POST /webhook/gitlab, /webhook/sync.
- POST /deploy/service/{svc}/gitlab.
- GET /deploy/service/{svc}/gitlab/status.
- POST /webhook/gitlab/test-host/{host}.
- POST /infra/plan/gitlab, /infra/apply/gitlab.

Hinweise:

- `/settings` exakt ist vom Core belegt (`api.py:115-118`).
- `/maintenance/*` ist bereits für Plugin-Pflege belegt. Für Wartungsfenster daher z. B.
  `/schedules`, `/overrides`, `/maintenance-windows` wählen.
- Es gibt keine Job-Detail- oder Promote-Route, und `/jobs` filtert nicht nach Status.

## 5. UI-Komponenten

React: `src/ui/PluginApp.tsx` (1628 Zeilen), `src/ui/lib/api.ts`, `src/ui/lib/hooks.ts`.

- `RUNNING_STATES` und `FAIL_STATES`, `statusColor`, `badgeVariant`, `StatusBadge`:
  `PluginApp.tsx:26-27, 56-86`. Unbekannte Status wie SCHEDULED fallen auf muted.
- `LogViewer`: `:235`.
- `ActivePipelines` (Jobs in RUNNING_STATES inkl. QUEUED): `:312-370`.
- `History` (Jobliste, Filter ID/Typ/Status): `:373-417`. Hier passt eine Fenster-Spalte oder ein
  Badge.
- `ServiceHistoryModal`: `:419`.
- `ServiceCatalog` (Deploy-Button mit `confirm`, ruft `iacApi.deployService` = `api.ts:255`): `:456`.
- `Overview`, `Provision`, `Assignments`: `:532, 691, 809`.
- `SettingsPage`, `AdvancedSettings`, `SchemaField`: `:1098, 983, 940`.
- `Dashboard` (Tabs `overview|active|provision|catalog|assignments|history`, `TabId` bei `:1301`;
  `NavTile` `:1310`, `ViewHeader` `:1338`): `:1441-1545`. Der `abort()`-Confirm (`:1469-1483`) ist
  das Vorbild für „Jetzt ausführen".
- `useJobsSSE`: `hooks.ts:16-86`. Der Snapshot enthält nur `get_recent_jobs(30)` (`api.py:1738-1750`).

Einhängepunkt „Geplante Deployments":

- Neuer Tab `scheduled` in `TabId` mit eigener Route. Der 30er-SSE-Snapshot reicht nicht, weil
  SCHEDULED-Jobs dort schnell verdrängt werden (`database.py:115-146`, Sortierung nach `id desc`).
- Der Button „Jetzt ausführen" braucht einen neuen Eintrag in `api.ts` neben `deployService`.

NiceGUI (parallel): `@ui.page('/iac')` in `entrypoint.py:323-326`, `app/ui/dashboard.py:16`
(Katalogkarten `:338`), `overview_dashboard.py:24`, `settings.py:16`, `terraform.py:168`, `widget.py:4`.

## 6. Teststand

- 33 Tests in `tests/`:
  - `test_queue.py` (13): Normalisierung, `dedupe_key_for`, `_conflicts`, enqueue/claim/duplicate,
    Heartbeat/Orphans, Log-Store, Worker auf SQLite-In-Memory mit `StaticPool`.
  - `test_inventory_publish.py` (6): `_build_inventory_hosts`, `_public_terraform`, `_attach_ci_ids`
    mit `AsyncMock`-`ctx.request`.
  - `test_rules.py` (6), `test_adopt_import.py` (5), `test_service.py` (3, Smoke).
  - Dazu `iac_core/tests/test_terraform_gen.py`.
- Fixtures (`tests/conftest.py:1-60`):
  - Es importiert `core.services` vor `core.api`.
  - Es registriert das Plugin als Paketalias `iac_plugin`; Tests importieren
    `iac_plugin.app.controller...`.
  - Es fügt `vendor/` zum `sys.path` hinzu.
- Fake-ctx und Engine: `MagicMock`-ctx (`test_service.py:12-28`,
  `ctx.subscribe = MagicMock(return_value=lambda fn: fn)`); Engine ohne `__init__` per
  `object.__new__(DeploymentEngine)` (`test_inventory_publish.py:10-14`); Queue-Tests patchen
  `queue_mod.db_instance`.
- Ausführung: im Container `lyndrix-core-dev` per `docker cp`, dann `PYTHONPATH=/app pytest -q tests`
  (`conftest.py:43`). Im Cluster-Bench zusätzlich `-e LYNDRIX_CLUSTER_MODE=single
  -e LYNDRIX_COORDINATOR=local`.
- Dev-Tools laut `requirements-dev.txt`: pytest, pytest-asyncio, pytest-cov, mypy, ruff, black.
- Es gibt keine Tests für `api.py`-Handler, die Webhook-Route oder `do_get_service_status`.

## 7. Cluster-Besonderheiten

- Auf jeder Instanz laufen Webhook-Router, Auth-Router, SSE und `enqueue_from_event`. Die
  Webhook-Dedupe `_recent_events` (TTL 90 s, `api.py:40, 216-219, 553-559`) ist pro Prozess im
  Speicher.
- Nur Runner (`service.py:55-65`) starten `JobWorker`, Startup-Reconciliation,
  `emit_monitoring_inventory_sync`, `emit_iac_inventory_updated` und Snapshots
  (`entrypoint.py:302-307`). `iac:inventory_updated` kommt daher nur von Runnern, und
  `NativeGenerateStage` läuft nur dort.
- Placement ist `any`, nicht `pinned:runner`. Es gibt kein 503/`X-Plugin-Runtime: remote` für dieses
  Plugin. Nicht-Runner antworten normal, Jobs liegen dann in der Queue (`runner_online()`,
  `service.py:67-75`).
- Es gibt einen Lease: `ctx.on_lease("iac:webhook_sync", ...)` (`entrypoint.py:313-317`, mit
  Fallback ohne `on_lease`). Für die Queue gibt es keinen Lease. Der Claim ist ein bedingtes
  `UPDATE ... WHERE status='QUEUED'` (`queue.py:242-250`).
- `ctx.set_status` wird nur als non-blocking `degraded` genutzt, wenn kein Runner online ist
  (`service.py:166-185`, Start `entrypoint.py:320`).
- Worker (`queue.py:662-736`):
  - Poll alle 2 s mit `wake`-Event.
  - `max_parallel` kommt aus dem Vault-Key `iac_max_parallel_jobs` (Default 3, `service.py:97`).
  - Heartbeat alle 10 s.
  - Orphan-Sweep alle 60 s, erster Sweep nach 90 s, `stale_seconds=180`.
  - `finalize_orphan` versucht den Abschluss aus Container-Exit-Codes (`service.py:111-115`), sonst
    FAILED „Runner '<owner>' lost" (`queue.py:355-364`).
- Jobabschluss: Der `finally`-Block in `engine.run_pipeline` ruft `db.update_job(final_status)`
  (`engine.py` rund um 1648-1660; `database.py:92-113`). Es gibt kein Completion-Event auf dem Bus,
  nur `ctx.notify("deployment_succeeded|failed")`.
- Zwei Deploys desselben Services kurz hintereinander:
  - `enqueue` wirft `DuplicateJob`, wenn ein QUEUED- oder RUNNING-Job mit gleichem `dedupe_key`
    existiert (`queue.py:193-199`).
  - `enqueue_from_event` loggt, sendet den Toast „Pipeline Ignored" und gibt `None` zurück
    (`service.py:129-138`). Der zweite, neuere Deploy wird also verworfen („erster gewinnt").
  - Zusätzlich gibt es den Guard in `run_pipeline` (`engine.py:1380-1402`): In-Memory-Set plus
    DB-Prüfung auf RUNNING, Treffer führt zu `_skip_claimed` (`engine.py:1745-1751`, Job wird ABORTED).

## 8. Einhängepunkte für Abschnitt 7.1

### 8.1 SSoT-Parsing und Vererbung veröffentlichen

Ist-Zustand:

- `iac_core` liest nur den iac-controller. Reihenfolge in `iac_core/app/services/loader.py:23-42`:
  global `01..04_*.yml`, dann `sites/<site>/{site_vars,networks,hardware}.yml`, dann
  `stages/<stage>/{stage_vars,stage,hosts}.yml`. Das Merge läuft per deepmerge, Listen werden
  dedupliziert. Danach folgen Jinja-Rendering mit zwei Pässen (`renderer.py:36-51`),
  `resolve_profiles` (`generator.py:55-89`) und Pydantic-Validierung (`validate/models.py:90-110`).
- `service.yml` wird von `iac_core` nicht gelesen. Das Plugin liest es nur in
  `_load_service_docker_names` (`engine.py:955-1014`) für `service.name`. `stage_overrides` kommt im
  Plugin nicht vor; das ist Template-Engine-Sache.
- Service-Repos werden im Job per `CloneServiceRepoStage` nach `services_dir/<service>` geklont
  (`stages/git.py:37-153`). Der Template-Engine-Container liest sie, nicht das Plugin.
- Effektive Kette im Plugin: Global, Site, Stage, dann pro Host `env_context` (global, site, stage;
  stage nicht für Hardware) mit dem Host obenauf (`generator.py:148-187`).
- Pass-through:
  - `StandardHost`, `HardwareNode`, `HardwareCluster`, `IaCConfiguration` und `TerraformConfig` haben
    `extra="allow"`. Ein Host-Key `maintenance` oder `incidents` überlebt also bis ins Inventory
    (`ansible_gen.py:173`).
  - `ServiceDefinition` (`validate/models.py:6-12`) hat kein `extra` (Pydantic ignoriert), unbekannte
    Keys an Service-Einträgen werden still verworfen.
  - Top-level-Keys außerhalb `hosts`/`hardware_hosts`/`*_vars` (`maintenance_default`,
    `incident_defaults`) werden nicht geschrieben (`ansible_gen.py:150-222`).
  - Liegt `maintenance_default` in `stage_vars`, landet es in `group_vars/all.yml`, aber `update()`
    überschreibt stage-übergreifend (`ansible_gen.py:155-158`).
- Das Service-Katalog-YAML `02_service_catalog.yml` wird roh an drei Stellen gelesen:
  `stages/ansible.py:22-40` (`deploy_type`), `stages/git.py:50-65, 160-175`, `api.py:608-614`.

Einhängepunkt:

- Neues Modul für `build_maintenance_items(...)`. Das Plugin hat kein `app/logic/`, es nutzt
  `app/controller/`.
- Aufruf direkt neben `emit_iac_inventory_updated` (`engine.py:1271-1295`), nach dem Emit bei
  `:1285`. Dort sind Hosts und `ci_id`s schon vorhanden. Emit von `maintenance:windows_published` mit
  `replaces=true`.
- Quelle Host und Stage: `_build_inventory_hosts` (`engine.py:1161-1194`) muss `maintenance` und
  `incidents` aus `host_data` aufnehmen. Heute werden nur ausgewählte Keys übernommen.
- Quelle Service:
  - `service.yml` liegt nur auf dem Runner unter `services_dir/<svc>/service.yml`.
  - Es ist nur für zuvor gesyncte Services vorhanden. `SyncAllServicesStage` läuft nur im `rollout`
    (`engine.py:859-860`).
  - Für „alle Services nach jedem Sync" braucht es `services_dir.rglob("service.yml")` (Muster
    `engine.py:999`, dort werden Fehler pro Datei still verschluckt) und Validierung gegen
    `parse_windows`.
- Service-`ci_id`:
  - Heute nicht vorhanden.
  - `_attach_ci_ids` (`engine.py:1196`) muss um Items mit `entity_type="service"` erweitert werden.
  - Dafür gibt es im Orchestrator keinen Präzedenzfall, der Identifier muss mit dem Registry-Plan
    abgestimmt werden (z. B. `iac.service = <host>/<service.name>`).
- Zeitpunkt „nach jedem Sync": Es gibt keinen Hook dafür. Nächstliegend sind `NativeGenerateStage`
  (`engine.py:143-153`) und der Boot (`entrypoint.py:306`). Beides läuft nur auf Runnern.

Risiken:

- `ServiceDefinition` verwirft Keys.
- `emit_iac_inventory_updated` fängt alle Exceptions und loggt nur (`engine.py:1293-1295`).
- Ein einziger `ctx.request` mit 10 s Timeout für alle Items.
- Event nur von Runner-Instanzen.
- Der HTTP-Pfad zum Monitoring (`engine.py:1101`) ist nicht um `ci_id` oder Fenster erweitert und
  läuft parallel.

### 8.2 SCHEDULED, Worker-Aufnahme, Zusammenfassen

Ist-Zustand:

- Status sind freie Strings. Aktive Schlüssel zählen nur QUEUED/RUNNING (`queue.py:170-181`,
  `enqueue` `:193-199`).
- `claim_next` nimmt den ältesten QUEUED nach `id` (`queue.py:235-247`) mit bedingtem UPDATE.
- `abort_queued` bricht alle QUEUED ab (`queue.py:387-406`).
- `live_summary` zählt `queued` (`queue.py:457-487`).
- Das Frontend kennt QUEUED in `RUNNING_STATES` (`PluginApp.tsx:26`).

Einhängepunkte:

1. `models.py:11-36` und `queue.py:148-155`: Spalten `not_before DATETIME NULL`,
   `window_ci_ids LONGTEXT NULL`, optional `override_id` und `schedule_reason`.
2. `queue.py`: Konstante `STATUS_SCHEDULED`. `enqueue(payload, *, not_before=None,
   window_ci_ids=None)` (`:184-221`) legt bei gesetztem `not_before` Status SCHEDULED an.
3. Aufnahme fälliger Jobs:
   - Kleinste Änderung ist `claim_next` (`queue.py:224-266`): Kandidatenabfrage um
     `OR (status='SCHEDULED' AND not_before <= now)` erweitern und das bedingte UPDATE (`:242-247`)
     auf `status IN (QUEUED, SCHEDULED)`. Der 2-s-Poll (`queue.py:733`) erledigt den Rest.
   - Alternative ist ein Promoter SCHEDULED zu QUEUED plus `iac:job_queued`, als Lease (Muster
     `entrypoint.py:313-317`) oder im Orphan-Sweep (`queue.py:727-729`).
   - `abort_queued`, `queued_count` und `live_summary` müssen bewusst behandeln, ob SCHEDULED
     mitzählt. Empfehlung: `abort_queued` bricht SCHEDULED nicht ab.
4. Zeitzone: DB-Zeiten sind naive lokale `datetime.now()` (`queue.py:241`, `database.py:101`).
   `not_before` muss konsistent geschrieben werden. Die Core-Bibliothek liefert tz-aware Zeiten, die
   vorher umzurechnen sind. Orphan-Code nutzt `hb.replace(tzinfo=None)` (`queue.py:329`).
5. Die Entscheidung „sofort oder geplant" gehört in `IaCService.enqueue_from_event`
   (`service.py:122-141`) bzw. `enqueue_pipeline` (`:77-90`), nicht in den öffentlichen Handler
   (`api.py:626-656`).
   - Ablauf: Wenn `pipeline_type=="single_service"` und die Quelle ein Webhook ist (nicht `manual`),
     `deploy_policy` ermitteln (prod gleich window, dev/test gleich immediate), Stage aus
     `service_branch` ableiten und das nächste Fenster über `next_start` berechnen.
   - Das Problem: Die Daten haben nur Runner (Git-Working-Trees), die Entscheidung fällt aber auf
     einer beliebigen Control-Plane-Instanz.
   - Vorhandenes Muster: Read-Model-Snapshots in `iac_orchestrator_state` (`publish_repo_snapshots`,
     `api.py:501-522`; Leser `_snapshot_items`, `:488-498`; Fallback `_has_repo_checkout`, `:484`).
     Effektive Fenster und Policies als weiteren Snapshot ablegen.
6. Zusammenfassen gibt es heute nicht (Duplikate werden verworfen, siehe Abschnitt 7). Bei
   vorhandenem SCHEDULED-Job mit gleichem `dedupe_key` müsste `enqueue` `payload` und `not_before`
   per UPDATE ersetzen statt `DuplicateJob` zu werfen (`queue.py:193-199`). `_conflicts` muss
   SCHEDULED getrennt behandeln. Läuft derselbe Service bereits (RUNNING), bleibt `DuplicateJob`.
7. CI-Rückmeldung: `do_get_service_status` (`api.py:659-694`) bildet SCHEDULED/QUEUED auf „unknown"
   ab. Es braucht einen eigenen Wert (z. B. `scheduled` plus `not_before`) und eine Anpassung im
   CI-Template, das außerhalb dieses Repos liegt.
8. Restart: `not_before` liegt in der DB und überlebt Runner-Restarts. `run_startup_reconciliation`
   (`service.py:271-322`) betrifft nur RUNNING.

Risiken:

- `pipeline_type` ist String(50).
- `get_latest_job_for_pipeline_type` filtert mit `since_epoch` auf `start_time`/`end_time`
  (`database.py:165-174`). `start_time` ist die Anlagezeit; ein lange geplanter Job wird nicht mehr
  gefunden.
- `stats.py` (`:18, 112-149`) zählt unbekannte Status nur in `by_status`.
- `clear_all_jobs(keep_running)` (`database.py:287-315`) löscht alle Nicht-RUNNING-Jobs, also auch
  SCHEDULED und QUEUED. Der Filter muss angepasst werden.

### 8.3 Override („Jetzt ausführen") und override_started/ended

Ist-Zustand:

- Es gibt keine Promote-Route.
- Das Jobende wird im `finally` von `engine.run_pipeline` bestimmt.

Einhängepunkte:

- Neue Route `POST /schedules/{job_id}/run-now` (write) in `app/api.py` mit Handler in
  `controller/api.py`.
  - Neue `JobQueue`-Methode: bedingtes UPDATE SCHEDULED zu QUEUED mit `override_id` (Muster
    `mark_failed`, `queue.py:336-353`).
  - Danach `ctx.emit("iac:job_queued", ..., cluster=True)` (Muster `service.py:80-90`) und
    `maintenance:override_started` mit `ci_ids=window_ci_ids`.
  - Das `until` ist eine Obergrenze (`now + max_override_minutes`), weil die Jobdauer vorab
    unbekannt ist.
- `override_ended` gehört in `engine.run_pipeline` `finally` (rund um `engine.py:1648-1660`), nur für
  Jobs mit `override_id`. Dieselbe Emission braucht es in `fail_orphans`/`mark_failed`
  (`queue.py:336-364`), im Abort-Pfad (`api.py:1130-1205`, `engine.abort_job` `engine.py:1932`) und
  in `_finalize_orphan`. Sonst bleibt ein Override bis `until` stehen.
- `cluster=True` ist nötig, weil Runner und Monitoring-Prober-Lease-Instanz verschieden sein können.
  `service.py:83` zeigt das TypeError-Fallback-Muster.
- Offen: Sollen auch manuelle Deploys (Katalog-Button) und `immediate`-Policy Overrides auslösen?

### 8.4 Default-Fenster-Schalter mit Git-Rückschreiben

Ist-Zustand Git-Schreibpfad:

- Das Plugin hat keinen eigenen Git-Client. Es delegiert per Bus an den Core (`git:sync`,
  `git:commit_push`, `engine.py:1981-2033`; Core: `git_service.py:319, 384`).
- Credentials: `repo_<slug>_config` (JSON mit `url`, `token_key`) aus Vault; das Token geht per
  `ctx.get_secret` als `secret_value` ins Event (`engine.py:2003-2019`).
- Der Commit-Autor wird vom Core gesetzt: `Actor("Lyndrix IaC", "iac@lyndrix.local")`
  (`git_service.py:270-271`). Der Plan nimmt „Lyndrix IaC Orchestrator" an.
- Der einzige Commit-Pfad ist `CommitPushStage("inventory_state", "ci: automated state update")`
  (`engine.py` rund um 1465, `stages/git.py:24-35`), also ins generierte Repo. `iac_controller` wird
  nur gelesen (`SyncRepoStage`).

Einhängepunkt:

- Neue Stage analog `CommitPushStage` für `role_slug="iac_controller"` mit diesen Schritten:
  1. `SyncRepoStage("iac_controller")`.
  2. Datei im Working Tree ändern:
     `git_repos_dir/iac_controller/environments/sites/<site>/stages/<stage>/stage*.yml`
     (`loader.py:36-41`).
  3. `execute_git_commit_push("iac_controller", msg)` (`engine.py:2003`).
- Sinnvoll als eigener Pipeline-Typ in `PipelineStrategy` (`engine.py:635-660`; Registry
  `get_pipeline_strategy` `:895`) mit `commit_state=False`. Dazu ein Key in `dedupe_key_for`
  (`queue.py:73-89`).
- Die Route kann `POST /maintenance-default` (write) heißen. Eine eigene Permission analog
  `api:infra_apply` wäre sinnvoll (`entrypoint.py:49-53`).

Risiken:

- Die Rolle `iac_controller` ist als read-only „SSoT Source" konfiguriert (`settings_schema.py`
  Repo-Rollen). Der Credential braucht Push-Recht, und Branch-Protection kann Push auf main
  verhindern. Einen Merge-Request-Weg gibt es im Plugin nicht.
- YAML-Roundtrip verliert Kommentare und Jinja-Formatierung und erzeugt Diff-Rauschen.
- Ein Push triggert Controller-CI. Der Schleifenschutz in `api.py` (rund um 268-270) gilt nur für
  „ci: automated state update".
- Die Plan-Pfade (`hosts.yml`, `stages/<stage>.yml`) weichen vom realen Layout ab.

### 8.5 deploy_policy-Default nach Stage

- Das Stage-Feld fehlt im Webhook-Payload (Abschnitt 3).
- Ableitung entweder aus `service_branch` mit der Logik von `SingleServiceStrategy`
  (`engine.py:666-672`), dann als gemeinsame Hilfsfunktion statt Kopie, oder aus den
  Inventory-Gruppen `stage_<x>` des Zielhosts (`engine.py:1037, 1191`).
- Die Quelle `service.yml` ist auf der Control Plane nicht lesbar und braucht den Snapshot aus 8.2
  Punkt 5. Der Katalog `02_service_catalog.yml` (`api.py:608-623`, mit Snapshot-Fallback) wäre eine
  zusätzliche, überall lesbare Quelle für `deploy_policy`.

## 9. Abweichungen vom Plan / offene Fragen

1. `service.yml` wird nicht vom Orchestrator geparst. Plan 7.1 nimmt eine Parsing-Stelle an, die es
   nicht gibt. Parsing, Validierung und Vererbung müssten neu entstehen. Alternativ liefert die
   Template-Engine effektive Werte nur ins Ansible-Inventory, das Plugin liest sie dann nicht.
2. Es gibt kein `stage_overrides` im Plugin. Die Kette dort ist Global, Site, Stage, Host (nur
   Controller).
3. `ServiceDefinition` verwirft unbekannte Keys (`validate/models.py:6-12`). Für
   `maintenance`/`incidents` an Service-Einträgen braucht es `extra="allow"` oder explizite Felder in
   der gebündelten `iac_core`.
4. Kein Stage-Feld im Webhook-Payload. Die Stage ist nur indirekt über Branch oder Inventory-Gruppe
   bestimmbar (`engine.py:666-672`).
5. Doppel-Deploys heute: erster gewinnt, nicht letzter Stand (`service.py:129-138`,
   `queue.py:193-199`). Die Zusammenfassungslogik ist neu und ändert bestehendes Verhalten.
6. CI-Statusvertrag: QUEUED/SCHEDULED gleich „unknown" (`api.py` Status-Mapping). Ein Deploy, der
   Stunden oder Tage geplant ist, passt nicht zu einem CI-Job, der auf das Jobende wartet. Der Vertrag
   mit dem aac-Service-CI-Template (außerhalb dieses Repos) fehlt im Plan. Dazu kommt `since_epoch`
   gegen `start_time` (Anlagezeit).
7. Commit-Autor ist im Core fest „Lyndrix IaC <iac@lyndrix.local>". Es gibt keinen Schreibpfad ins
   Controller-Repo, und Schreibrecht, Branch-Protection und YAML-Roundtrip sind ungeklärt.
8. Controller-Pfade im Plan (`hosts.yml`, `stages/<stage>.yml`) stimmen nicht mit
   `environments/sites/<site>/stages/<stage>/{stage_vars,stage,hosts}.yml` überein.
   `maintenance_default` außerhalb `stage_vars` wird nicht ins Inventory geschrieben, und Stage-Werte
   in `group_vars/all.yml` überschreiben sich.
9. `iac:inventory_updated` enthält nur Hosts (kein `services`, keine Service-`ci_id`) und kommt nur von
   Runnern. Der Plan erwartet `kind: host|vm|service`. Das Plugin kennt nur `entity_type`
   host|hypervisor (`engine.py:1181`); die container/vm-Unterscheidung steckt nur im
   Server-Manager-Mapping (`engine.py:1250-1255`).
10. Struktur: Das Plugin hat noch kein `app/logic/`, es nutzt `app/controller/`. `PLUGIN_VERSION` ist
    doppelt gepflegt (`api.py:35`, `entrypoint.py:23`).
11. Nicht-Runner-Entscheidung: SCHEDULED oder QUEUED muss auf der annehmenden Control-Plane-Instanz
    fallen, die Daten hat nur der Runner. Vorschlag ist der DB-Snapshot (Muster
    `publish_repo_snapshots`). Im Plan ist das nicht vorgesehen.
12. Override-Ende ist vorab unbekannt. Plan „Ende = Jobende + Puffer" kollidiert mit dem sofortigen
    `override_started`. Vorschlag: `until = now + max_override_minutes` (4 h) und `override_ended`
    beim Abschluss. Es fehlt ein Bus-Completion-Event, der Emit muss in `run_pipeline`, Orphan und
    Abort ergänzt werden.
13. Settings: kein `settings_schema` im Manifest, kein `ctx.get_setting`, keine Settings-Actions.
    - Es gibt `SettingField`-Felder (`controller/settings_schema.py:55-97`, `_build_fields()`
      `:99-209`), rund 30 Felder in Kategorien Pipeline, GitLab Webhooks, Ansible, Terraform und
      Repo-Rollen.
    - Gespeichert wird in Vault über `ctx.get_secret`/`set_secret`. Env-Variablen
      `PLUGIN_IAC_ORCHESTRATOR_*` haben Vorrang (`config.py:9-24`).
    - Neue Settings (`override_buffer_minutes`, `max_override_minutes`) gehören in `_build_fields()`
      und werden von `SchemaField` (`PluginApp.tsx:940`) automatisch gerendert.
    - Der Default-Schalter ist besser als eigene Route plus Button als als Feld.
14. AI-Doku: `ai/INSTRUCTIONS.md` (32 Zeilen, Englisch) und die Route `/ai/instructions`
    (`api.py:50-58`) existieren. Sie sowie `documentation.md` (Abschnitte „API-Referenz" `:99` und
    „Events" `:142`, Deutsch) und `CHANGELOG.md` müssen für Scheduled-Deploys, Override und neue
    Events ergänzt werden.
15. Nicht gefunden: Der Quellcode der Service-CI (`prod-trigger-orchestrator`) liegt nicht in diesem
    Repo. `LYNDRIX_DIRECT_TRIGGER_ENABLED` steht nur im Kommentar `gitlab_webhooks.py:155-158`.
    Renovate ruft den Orchestrator nicht direkt auf; die Service-CI triggert.
