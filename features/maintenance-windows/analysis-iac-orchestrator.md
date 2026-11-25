# Analysis sweep P0: IaC Orchestrator (`lyndrix-plugin-iac-orchestrator`)

State 2026-10-17, read-only (Sonnet), on section 7.1 / 10 of the plan. The sweep returned the text
as a report; this file is the unchanged archive copy. Paths relative to
`plugins/lyndrix-plugin-iac-orchestrator/`.

## 1. Overview

- Manifest ID `lyndrix.plugin.iac_orchestrator` (`entrypoint.py:21`). Version `1.5.0`
  (`entrypoint.py:23`). A second copy `PLUGIN_VERSION` is in `app/api.py:35` and must be kept in
  sync.
- `min_core_version` is `1.4.0` (`entrypoint.py:28`). If `core.api.maintenance` (API 1.13) is
  imported, either raise `min_core_version` or import it via try/except.
- Placement is `any`. The control plane runs on every instance, execution only on instances
  with the role `runner` (`entrypoint.py:29-34`, `app/controller/service.py:55-65`).
- `permissions.subscribe` (`entrypoint.py:102-103`): `vault:ready_for_data`, `iac:webhook_verified`,
  `git:status_update`, `db:connected`, `socket:response`, `entity_registry:identified`,
  `iac:job_queued`, `iac:job_abort_requested`.
- `permissions.emit` (`entrypoint.py:104-108`): `iac:pipeline_started`, `iac:webhook_verified`,
  `iac:inventory_updated`, `entity_registry:identify`, `server_manager:register_server`, `git:sync`,
  `git:commit_push`, `system:notify`, `user:notify`, `socket:request`, `messaging:outbound`,
  `iac:job_queued`, `iac:job_abort_requested`.
- The topics `maintenance:windows_published`, `maintenance:override_started` and
  `maintenance:override_ended` must be added to `permissions.emit`.
- Roles `admin`, `operator`, `viewer` and the custom permission `api:infra_apply` are in
  `entrypoint.py:49-80`.
- The manifest has no `settings_schema`, no panels and no nav entries. It has
  `ai_instructions` and `ai_instructions_route` (`entrypoint.py:37-43`) as well as
  `settings_ui_route="/iac/settings"` (`entrypoint.py:100`).

## 2. Data model

Models in `app/model/models.py`, access in `app/model/database.py` (`JobDatabase`) and
`app/controller/queue.py` (`JobQueue`).

**`iac_orchestrator_jobs`** (`models.py:11-36`, `IaCJob`)

- `id`: Integer PK.
- `pipeline_type`: String(50), e.g. `single_service:<svc>` (`engine.py:663`, `queue.py:104-111`).
  There is a length risk for long service names.
- `status`: String(20), default RUNNING. Values set: QUEUED, RUNNING, SUCCESS, FAILED, ERROR,
  ABORTED (`queue.py:30-33`, `database.py:100`).
- `progress` and `current_step`.
- `start_time` (server_default now) and `end_time`. For QUEUED jobs `start_time` is the enqueue time.
- `logs` (legacy) and `pending_tasks`.
- `payload`: trigger payload as JSON (`queue.py:207`).
- `dedupe_key`: String(255) (`queue.py:73-89`).
- `owner_instance`, `claimed_at`, `heartbeat_at`.
- `runners`: live snapshot as JSON.

Further tables:

- `iac_orchestrator_job_logs`: one row per log line, with `seq`.
- `iac_orchestrator_job_tasks`.
- `iac_orchestrator_state`: snapshots, `commit_hash`.

Schema migration:

- `JobQueue.ensure_schema()` (`queue.py:137-167`) creates columns via `ALTER TABLE ADD COLUMN`
  (dict `wanted`, `queue.py:148-155`). This runs under `advisory_lock("lyndrix_bootstrap")`, error
  1060 is tolerated.
- It is called from `IaCService.bootstrap_db` (`service.py:209-225`; boot and `db:connected`,
  `entrypoint.py:274-278`).
- New columns (`not_before`, `window_ci_ids`, `override_id`) belong in `wanted` and in the ORM model.
  The SQLite variant is replaced automatically (`queue.py:156-158`).

`ci_id` usage:

- `ci_id` exists only for hosts, only in the event `iac:inventory_updated` and in
  `server_manager:register_server` (`engine.py:1196-1269`).
- It is not persisted in the DB and is not attached to jobs.
- Services have no `ci_id` in the plugin.
- Strong keys: `iac.host`, `pve.vm` (`node_name/vm_id`) and `fqdn`, the latter only if the host name
  contains a dot (`engine.py:1212-1218`).

## 3. Events

**Emit**

- `iac:webhook_verified`:
  - Sources: `controller/api.py:577, 654, 736, 784, 798, 813, 854, 1118` as well as the
    host rollout handoff in `engine.py`.
  - Payload: `{pipeline_type, service_name, service_branch, manual, trigger?, ...}`.
  - It is processed locally in `entrypoint.py:283-287`, there via `enqueue_from_event`
    (`service.py:122-141`).
- `iac:job_queued` (`service.py:83`, `cluster=True`): payload `{job_id}`.
- `iac:job_abort_requested` (`api.py:1164-1166`): payload `{job_ids}`.
- `iac:inventory_updated` (`engine.py:1271-1295`, emit at `:1285`):
  - Payload: `{"hosts":[{name, hostname, ip, groups, baseline_roles, terraform (without secrets),
    entity_type host|hypervisor, site, stage, ci_id}]}`.
  - There is no `services` field and no service `ci_id`.
  - Triggers: boot, only on runners (`entrypoint.py:306`), and `NativeGenerateStage`
    (`engine.py:143-153`), i.e. after every pipeline with a generate prefix, also `single_service`.
- `server_manager:register_server` (`engine.py:1245-1269`): per host with `ci_id`.
- `entity_registry:identify` via `ctx.request` (`engine.py:1196-1243`, call at `:1226`):
  - Items: `{entity_type, identifiers, display_name, attributes, source:"iac-orchestrator"}`.
  - Reply topic is `entity_registry:identified`, timeout 10 s.
  - Reply: `{ok, results:[{ok, uuid, conflict, error}]}`.
  - Without `ctx.request` or without a reply, `ci_id=None` remains (`engine.py:1205-1207, 1228-1231`).
- `git:sync` and `git:commit_push` (`engine.py:1989, 2022`): request/response via `request_id` and
  `git:status_update`. Core handlers: `lyndrix-core/app/core/components/git/logic/git_service.py:319, 384`.
- `messaging:outbound` and `system:notify` (`api.py:69, 312, 1197`) as well as `ctx.notify` in `engine.py`.

The monitoring sync is not a bus event:

- `POST http://127.0.0.1:<PORT>/api/plugins/lyndrix.plugin.state_monitoring/inventory-sync` with
  `LYNDRIX_SYSTEM_API_KEY` (`engine.py:1101-1143`).
- Payload `{owner_source, source_revision, hosts[], services[]}` (`engine.py:1016-1089`).
- Stage and site are derived from the inventory groups `stage_*`/`site_*`.
- Services appear here without `ci_id`.

**Subscribe**

- `db:connected` (`entrypoint.py:276`).
- `iac:webhook_verified` (`entrypoint.py:283-287`). Copies with `_origin` are ignored.
- `iac:job_queued` (`entrypoint.py:290-293`) calls `worker.wake()`.
- `iac:job_abort_requested` (`entrypoint.py:296-298`).
- `git:status_update` (subscribe in the `engine.py` `__init__`, handler `engine.py:1961`).
- `vault:ready_for_data`, `socket:response` and `entity_registry:identified` appear only in the
  permissions and are not subscribed via `ctx.subscribe`.

**Webhook path up to the job**

1. The public router `/api/iac` hangs directly on `main.app` (`entrypoint.py:251-259`), handlers in
   `app/controller/webhook_router.py:23-82`.
2. Validation: `_require_gitlab_token` (`controller/api.py:94-103`) compares `X-Gitlab-Token` via
   `hmac.compare_digest` with the Vault secret `gitlab_webhook_token`. If the token is missing in the Vault, it returns
   500, on mismatch 401.
3. The Renovate/prod path is not the GitLab group webhook but the service CI job
   `prod-trigger-orchestrator`:
   - It calls `POST /api/iac/deploy/service/{svc}/gitlab` with body `{"branch":"main"}`
     (`webhook_router.py:45-52`, `do_trigger_service_deployment` in `api.py:626-656`).
   - The group webhook `/webhook/gitlab` (`api.py:543-585`) has `pipeline_events=False`
     (`gitlab_webhooks.py:155-159`, deliberately against double deploys). `_should_trigger_orchestrator`
     (`api.py:252-278`) never lets merge_request, push etc. trigger.
4. Identification in the payload:
   - The service is `service_name` (path segment, lowercase, validated).
   - The branch is `service_branch`.
   - There is no explicit stage field and no host in the payload.
   - The stage follows implicitly from the branch: `SingleServiceStrategy` maps `dev` or `*-dev` to
     `stage_dev`, `test` to `stage_test`, otherwise `service_<name>` (`engine.py:666-672`). The host
     results from the generated inventory.
5. Event payload (`api.py:647-653`): `{pipeline_type:"single_service", service_name, service_branch,
   manual:True}`. A `trigger` field is missing, so `requested_by` becomes "ui" (`service.py:128`).
   - Afterwards: `iac:webhook_verified`, `enqueue_from_event`, `JobQueue.enqueue`, row QUEUED,
     `iac:job_queued`.
6. The CI polls `GET /api/iac/deploy/service/{svc}/gitlab/status?since_epoch=` (`api.py:659-694`):
   - SUCCESS becomes `success`, FAILED/ERROR/ABORTED become `failed`, RUNNING/PENDING become `running`.
   - Everything else, i.e. also QUEUED and in future SCHEDULED, becomes `"unknown"`.
   - The search runs via `pipeline_type == "single_service:<svc>"` with a time window on
     `start_time`/`end_time` (`database.py:148-191`).

## 4. API routes

Authenticated under `/api/plugins/lyndrix.plugin.iac_orchestrator/` (`app/api.py`). READ is
`plugin:<id>:api:read`, WRITE is `plugin:<id>:api:write`.

| Method | Path | Permission | Line |
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
| POST | /deploy/service/{svc} (body {branch}) | write | 212 |
| POST | /deploy/test-host/{host}, /bootstrap/{host} | write | 220, 228 |
| POST | /infra/plan | write | 236 |
| POST | /infra/pending-plan/apply, /infra/apply | write + api:infra_apply | 249, 266 |
| POST | /infra/pending-plan/dismiss | write | 256 |
| POST | /stream/ticket | read | 276 |

Directly on the app, auth in the handler:

- `GET /api/plugins/<id>/stream/jobs` (SSE, `?ticket=` or `?token=`, `api.py:293`).
- `GET .../jobs/{id}/logs/raw` (`api.py:301`).

Public under `/api/iac` with `X-Gitlab-Token` (`webhook_router.py:33-80`):

- GET /health.
- POST /webhook/gitlab, /webhook/sync.
- POST /deploy/service/{svc}/gitlab.
- GET /deploy/service/{svc}/gitlab/status.
- POST /webhook/gitlab/test-host/{host}.
- POST /infra/plan/gitlab, /infra/apply/gitlab.

Notes:

- `/settings` exactly is taken by the core (`api.py:115-118`).
- `/maintenance/*` is already taken for plugin upkeep. For maintenance windows, therefore choose e.g.
  `/schedules`, `/overrides`, `/maintenance-windows`.
- There is no job detail or promote route, and `/jobs` does not filter by status.

## 5. UI components

React: `src/ui/PluginApp.tsx` (1628 lines), `src/ui/lib/api.ts`, `src/ui/lib/hooks.ts`.

- `RUNNING_STATES` and `FAIL_STATES`, `statusColor`, `badgeVariant`, `StatusBadge`:
  `PluginApp.tsx:26-27, 56-86`. Unknown statuses such as SCHEDULED fall back to muted.
- `LogViewer`: `:235`.
- `ActivePipelines` (jobs in RUNNING_STATES incl. QUEUED): `:312-370`.
- `History` (job list, filter ID/type/status): `:373-417`. A window column or a
  badge fits here.
- `ServiceHistoryModal`: `:419`.
- `ServiceCatalog` (deploy button with `confirm`, calls `iacApi.deployService` = `api.ts:255`): `:456`.
- `Overview`, `Provision`, `Assignments`: `:532, 691, 809`.
- `SettingsPage`, `AdvancedSettings`, `SchemaField`: `:1098, 983, 940`.
- `Dashboard` (tabs `overview|active|provision|catalog|assignments|history`, `TabId` at `:1301`;
  `NavTile` `:1310`, `ViewHeader` `:1338`): `:1441-1545`. The `abort()` confirm (`:1469-1483`) is
  the model for "Run now".
- `useJobsSSE`: `hooks.ts:16-86`. The snapshot only contains `get_recent_jobs(30)` (`api.py:1738-1750`).

Attachment point "Scheduled deployments":

- A new tab `scheduled` in `TabId` with its own route. The 30-job SSE snapshot is not enough, because
  SCHEDULED jobs are quickly displaced there (`database.py:115-146`, sorted by `id desc`).
- The "Run now" button needs a new entry in `api.ts` next to `deployService`.

NiceGUI (parallel): `@ui.page('/iac')` in `entrypoint.py:323-326`, `app/ui/dashboard.py:16`
(catalog cards `:338`), `overview_dashboard.py:24`, `settings.py:16`, `terraform.py:168`, `widget.py:4`.

## 6. Test status

- 33 tests in `tests/`:
  - `test_queue.py` (13): normalisation, `dedupe_key_for`, `_conflicts`, enqueue/claim/duplicate,
    heartbeat/orphans, log store, worker on SQLite in-memory with `StaticPool`.
  - `test_inventory_publish.py` (6): `_build_inventory_hosts`, `_public_terraform`, `_attach_ci_ids`
    with an `AsyncMock` `ctx.request`.
  - `test_rules.py` (6), `test_adopt_import.py` (5), `test_service.py` (3, smoke).
  - Plus `iac_core/tests/test_terraform_gen.py`.
- Fixtures (`tests/conftest.py:1-60`):
  - It imports `core.services` before `core.api`.
  - It registers the plugin as the package alias `iac_plugin`; tests import
    `iac_plugin.app.controller...`.
  - It adds `vendor/` to `sys.path`.
- Fake ctx and engine: `MagicMock` ctx (`test_service.py:12-28`,
  `ctx.subscribe = MagicMock(return_value=lambda fn: fn)`); engine without `__init__` via
  `object.__new__(DeploymentEngine)` (`test_inventory_publish.py:10-14`); queue tests patch
  `queue_mod.db_instance`.
- Running: in the container `lyndrix-core-dev` via `docker cp`, then `PYTHONPATH=/app pytest -q tests`
  (`conftest.py:43`). On the cluster bench additionally `-e LYNDRIX_CLUSTER_MODE=single
  -e LYNDRIX_COORDINATOR=local`.
- Dev tools per `requirements-dev.txt`: pytest, pytest-asyncio, pytest-cov, mypy, ruff, black.
- There are no tests for `api.py` handlers, the webhook route or `do_get_service_status`.

## 7. Cluster specifics

- On every instance run the webhook router, auth router, SSE and `enqueue_from_event`. The
  webhook dedupe `_recent_events` (TTL 90 s, `api.py:40, 216-219, 553-559`) is per process in
  memory.
- Only runners (`service.py:55-65`) start `JobWorker`, startup reconciliation,
  `emit_monitoring_inventory_sync`, `emit_iac_inventory_updated` and snapshots
  (`entrypoint.py:302-307`). `iac:inventory_updated` therefore only comes from runners, and
  `NativeGenerateStage` only runs there.
- Placement is `any`, not `pinned:runner`. There is no 503/`X-Plugin-Runtime: remote` for this
  plugin. Non-runners answer normally, jobs then sit in the queue (`runner_online()`,
  `service.py:67-75`).
- There is a lease: `ctx.on_lease("iac:webhook_sync", ...)` (`entrypoint.py:313-317`, with a
  fallback without `on_lease`). There is no lease for the queue. The claim is a conditional
  `UPDATE ... WHERE status='QUEUED'` (`queue.py:242-250`).
- `ctx.set_status` is only used as non-blocking `degraded` if no runner is online
  (`service.py:166-185`, start `entrypoint.py:320`).
- Worker (`queue.py:662-736`):
  - Poll every 2 s with a `wake` event.
  - `max_parallel` comes from the Vault key `iac_max_parallel_jobs` (default 3, `service.py:97`).
  - Heartbeat every 10 s.
  - Orphan sweep every 60 s, first sweep after 90 s, `stale_seconds=180`.
  - `finalize_orphan` tries to conclude from container exit codes (`service.py:111-115`), otherwise
    FAILED "Runner '<owner>' lost" (`queue.py:355-364`).
- Job completion: the `finally` block in `engine.run_pipeline` calls `db.update_job(final_status)`
  (`engine.py` around 1648-1660; `database.py:92-113`). There is no completion event on the bus,
  only `ctx.notify("deployment_succeeded|failed")`.
- Two deploys of the same service in quick succession:
  - `enqueue` raises `DuplicateJob` if a QUEUED or RUNNING job with the same `dedupe_key`
    exists (`queue.py:193-199`).
  - `enqueue_from_event` logs, sends the toast "Pipeline Ignored" and returns `None`
    (`service.py:129-138`). The second, newer deploy is therefore discarded ("first wins").
  - In addition there is the guard in `run_pipeline` (`engine.py:1380-1402`): in-memory set plus
    DB check on RUNNING, a hit leads to `_skip_claimed` (`engine.py:1745-1751`, job becomes ABORTED).

## 8. Attachment points for section 7.1

### 8.1 Publishing SSoT parsing and inheritance

Current state:

- `iac_core` only reads the iac-controller. Order in `iac_core/app/services/loader.py:23-42`:
  global `01..04_*.yml`, then `sites/<site>/{site_vars,networks,hardware}.yml`, then
  `stages/<stage>/{stage_vars,stage,hosts}.yml`. The merge runs via deepmerge, lists are
  deduplicated. After that come Jinja rendering with two passes (`renderer.py:36-51`),
  `resolve_profiles` (`generator.py:55-89`) and Pydantic validation (`validate/models.py:90-110`).
- `service.yml` is not read by `iac_core`. The plugin only reads it in
  `_load_service_docker_names` (`engine.py:955-1014`) for `service.name`. `stage_overrides` does not appear in the
  plugin; that is a template engine matter.
- Service repos are cloned in the job via `CloneServiceRepoStage` to `services_dir/<service>`
  (`stages/git.py:37-153`). The template engine container reads them, not the plugin.
- Effective chain in the plugin: global, site, stage, then per host `env_context` (global, site, stage;
  stage not for hardware) with the host on top (`generator.py:148-187`).
- Pass-through:
  - `StandardHost`, `HardwareNode`, `HardwareCluster`, `IaCConfiguration` and `TerraformConfig` have
    `extra="allow"`. A host key `maintenance` or `incidents` therefore survives into the inventory
    (`ansible_gen.py:173`).
  - `ServiceDefinition` (`validate/models.py:6-12`) has no `extra` (Pydantic ignores), unknown
    keys on service entries are silently discarded.
  - Top-level keys outside `hosts`/`hardware_hosts`/`*_vars` (`maintenance_default`,
    `incident_defaults`) are not written (`ansible_gen.py:150-222`).
  - If `maintenance_default` is in `stage_vars`, it ends up in `group_vars/all.yml`, but `update()`
    overwrites across stages (`ansible_gen.py:155-158`).
- The service catalog YAML `02_service_catalog.yml` is read raw in three places:
  `stages/ansible.py:22-40` (`deploy_type`), `stages/git.py:50-65, 160-175`, `api.py:608-614`.

Attachment point:

- A new module for `build_maintenance_items(...)`. The plugin has no `app/logic/`, it uses
  `app/controller/`.
- Called directly next to `emit_iac_inventory_updated` (`engine.py:1271-1295`), after the emit at
  `:1285`. Hosts and `ci_id`s are already available there. Emit `maintenance:windows_published` with
  `replaces=true`.
- Source host and stage: `_build_inventory_hosts` (`engine.py:1161-1194`) must take `maintenance` and
  `incidents` from `host_data`. Today only selected keys are adopted.
- Source service:
  - `service.yml` exists only on the runner under `services_dir/<svc>/service.yml`.
  - It is only present for previously synced services. `SyncAllServicesStage` runs only in `rollout`
    (`engine.py:859-860`).
  - For "all services after every sync" `services_dir.rglob("service.yml")` is needed (pattern
    `engine.py:999`, where errors per file are silently swallowed) and validation against
    `parse_windows`.
- Service `ci_id`:
  - Not present today.
  - `_attach_ci_ids` (`engine.py:1196`) must be extended by items with `entity_type="service"`.
  - There is no precedent for this in the orchestrator; the identifier must be coordinated with the registry plan
    (e.g. `iac.service = <host>/<service.name>`).
- Timing "after every sync": there is no hook for it. The nearest candidates are `NativeGenerateStage`
  (`engine.py:143-153`) and boot (`entrypoint.py:306`). Both run only on runners.

Risks:

- `ServiceDefinition` discards keys.
- `emit_iac_inventory_updated` catches all exceptions and only logs (`engine.py:1293-1295`).
- A single `ctx.request` with a 10 s timeout for all items.
- Event only from runner instances.
- The HTTP path to monitoring (`engine.py:1101`) is not extended by `ci_id` or windows and
  runs in parallel.

### 8.2 SCHEDULED, worker pickup, merging

Current state:

- Statuses are free strings. Active keys only count QUEUED/RUNNING (`queue.py:170-181`,
  `enqueue` `:193-199`).
- `claim_next` takes the oldest QUEUED by `id` (`queue.py:235-247`) with a conditional UPDATE.
- `abort_queued` aborts all QUEUED (`queue.py:387-406`).
- `live_summary` counts `queued` (`queue.py:457-487`).
- The frontend knows QUEUED in `RUNNING_STATES` (`PluginApp.tsx:26`).

Attachment points:

1. `models.py:11-36` and `queue.py:148-155`: columns `not_before DATETIME NULL`,
   `window_ci_ids LONGTEXT NULL`, optionally `override_id` and `schedule_reason`.
2. `queue.py`: constant `STATUS_SCHEDULED`. `enqueue(payload, *, not_before=None,
   window_ci_ids=None)` (`:184-221`) creates status SCHEDULED when `not_before` is set.
3. Picking up due jobs:
   - The smallest change is `claim_next` (`queue.py:224-266`): extend the candidate query by
     `OR (status='SCHEDULED' AND not_before <= now)` and the conditional UPDATE (`:242-247`)
     to `status IN (QUEUED, SCHEDULED)`. The 2 s poll (`queue.py:733`) does the rest.
   - The alternative is a promoter SCHEDULED to QUEUED plus `iac:job_queued`, as a lease (pattern
     `entrypoint.py:313-317`) or in the orphan sweep (`queue.py:727-729`).
   - `abort_queued`, `queued_count` and `live_summary` must deliberately decide whether SCHEDULED
     counts. Recommendation: `abort_queued` does not abort SCHEDULED.
4. Time zone: DB times are naive local `datetime.now()` (`queue.py:241`, `database.py:101`).
   `not_before` must be written consistently. The core library returns tz-aware times, which have to be
   converted beforehand. Orphan code uses `hb.replace(tzinfo=None)` (`queue.py:329`).
5. The decision "immediate or scheduled" belongs in `IaCService.enqueue_from_event`
   (`service.py:122-141`) or `enqueue_pipeline` (`:77-90`), not in the public handler
   (`api.py:626-656`).
   - Flow: if `pipeline_type=="single_service"` and the source is a webhook (not `manual`),
     determine `deploy_policy` (prod equals window, dev/test equals immediate), derive the stage from
     `service_branch` and compute the next window via `next_start`.
   - The problem: only runners have the data (git working trees), but the decision is made
     on an arbitrary control-plane instance.
   - Existing pattern: read-model snapshots in `iac_orchestrator_state` (`publish_repo_snapshots`,
     `api.py:501-522`; reader `_snapshot_items`, `:488-498`; fallback `_has_repo_checkout`, `:484`).
     Store effective windows and policies as a further snapshot.
6. Merging does not exist today (duplicates are discarded, see section 7). With an
   existing SCHEDULED job with the same `dedupe_key`, `enqueue` would have to replace `payload` and `not_before`
   via UPDATE instead of raising `DuplicateJob` (`queue.py:193-199`). `_conflicts` must treat
   SCHEDULED separately. If the same service is already running (RUNNING), `DuplicateJob` remains.
7. CI feedback: `do_get_service_status` (`api.py:659-694`) maps SCHEDULED/QUEUED to "unknown".
   It needs its own value (e.g. `scheduled` plus `not_before`) and an adjustment in the
   CI template, which lies outside this repo.
8. Restart: `not_before` lives in the DB and survives runner restarts. `run_startup_reconciliation`
   (`service.py:271-322`) only concerns RUNNING.

Risks:

- `pipeline_type` is String(50).
- `get_latest_job_for_pipeline_type` filters with `since_epoch` on `start_time`/`end_time`
  (`database.py:165-174`). `start_time` is the creation time; a job scheduled for a long time is no longer
  found.
- `stats.py` (`:18, 112-149`) counts unknown statuses only in `by_status`.
- `clear_all_jobs(keep_running)` (`database.py:287-315`) deletes all non-RUNNING jobs, i.e. also
  SCHEDULED and QUEUED. The filter must be adapted.

### 8.3 Override ("Run now") and override_started/ended

Current state:

- There is no promote route.
- The job end is determined in the `finally` of `engine.run_pipeline`.

Attachment points:

- New route `POST /schedules/{job_id}/run-now` (write) in `app/api.py` with a handler in
  `controller/api.py`.
  - New `JobQueue` method: conditional UPDATE SCHEDULED to QUEUED with `override_id` (pattern
    `mark_failed`, `queue.py:336-353`).
  - Afterwards `ctx.emit("iac:job_queued", ..., cluster=True)` (pattern `service.py:80-90`) and
    `maintenance:override_started` with `ci_ids=window_ci_ids`.
  - The `until` is an upper bound (`now + max_override_minutes`), because the job duration is
    unknown in advance.
- `override_ended` belongs in `engine.run_pipeline` `finally` (around `engine.py:1648-1660`), only for
  jobs with `override_id`. The same emission is needed in `fail_orphans`/`mark_failed`
  (`queue.py:336-364`), in the abort path (`api.py:1130-1205`, `engine.abort_job` `engine.py:1932`) and
  in `_finalize_orphan`. Otherwise an override stays in place until `until`.
- `cluster=True` is necessary because the runner and the monitoring prober lease instance can differ.
  `service.py:83` shows the TypeError fallback pattern.
- Open: should manual deploys (catalog button) and the `immediate` policy also trigger overrides?

### 8.4 Default window switch with git write-back

Current state of the git write path:

- The plugin has no git client of its own. It delegates via the bus to the core (`git:sync`,
  `git:commit_push`, `engine.py:1981-2033`; core: `git_service.py:319, 384`).
- Credentials: `repo_<slug>_config` (JSON with `url`, `token_key`) from the Vault; the token goes via
  `ctx.get_secret` as `secret_value` into the event (`engine.py:2003-2019`).
- The commit author is set by the core: `Actor("Lyndrix IaC", "iac@lyndrix.local")`
  (`git_service.py:270-271`). The plan assumes "Lyndrix IaC Orchestrator".
- The only commit path is `CommitPushStage("inventory_state", "ci: automated state update")`
  (`engine.py` around 1465, `stages/git.py:24-35`), i.e. into the generated repo. `iac_controller` is
  only read (`SyncRepoStage`).

Attachment point:

- A new stage analogous to `CommitPushStage` for `role_slug="iac_controller"` with these steps:
  1. `SyncRepoStage("iac_controller")`.
  2. Change the file in the working tree:
     `git_repos_dir/iac_controller/environments/sites/<site>/stages/<stage>/stage*.yml`
     (`loader.py:36-41`).
  3. `execute_git_commit_push("iac_controller", msg)` (`engine.py:2003`).
- Sensible as its own pipeline type in `PipelineStrategy` (`engine.py:635-660`; registry
  `get_pipeline_strategy` `:895`) with `commit_state=False`. Plus a key in `dedupe_key_for`
  (`queue.py:73-89`).
- The route can be called `POST /maintenance-default` (write). A separate permission analogous to
  `api:infra_apply` would make sense (`entrypoint.py:49-53`).

Risks:

- The role `iac_controller` is configured as a read-only "SSoT source" (`settings_schema.py`
  repo roles). The credential needs push rights, and branch protection may
  prevent pushing to main. There is no merge-request path in the plugin.
- A YAML roundtrip loses comments and Jinja formatting and creates diff noise.
- A push triggers the controller CI. The loop protection in `api.py` (around 268-270) only applies to
  "ci: automated state update".
- The plan paths (`hosts.yml`, `stages/<stage>.yml`) deviate from the real layout.

### 8.5 deploy_policy default by stage

- The stage field is missing in the webhook payload (section 3).
- Derivation either from `service_branch` with the logic of `SingleServiceStrategy`
  (`engine.py:666-672`), then as a shared helper function instead of a copy, or from the
  inventory groups `stage_<x>` of the target host (`engine.py:1037, 1191`).
- The source `service.yml` is not readable on the control plane and needs the snapshot from 8.2
  item 5. The catalog `02_service_catalog.yml` (`api.py:608-623`, with snapshot fallback) would be an
  additional source for `deploy_policy` that is readable everywhere.

## 9. Deviations from the plan / open questions

1. `service.yml` is not parsed by the orchestrator. Plan 7.1 assumes a parsing location that does
   not exist. Parsing, validation and inheritance would have to be created. Alternatively the
   template engine delivers effective values only into the Ansible inventory, and the plugin then does not read them.
2. There is no `stage_overrides` in the plugin. The chain there is global, site, stage, host (controller
   only).
3. `ServiceDefinition` discards unknown keys (`validate/models.py:6-12`). For
   `maintenance`/`incidents` on service entries, `extra="allow"` or explicit fields are needed in
   the bundled `iac_core`.
4. No stage field in the webhook payload. The stage can only be determined indirectly via branch or inventory group
   (`engine.py:666-672`).
5. Double deploys today: first wins, not the latest state (`service.py:129-138`,
   `queue.py:193-199`). The merge logic is new and changes existing behaviour.
6. CI status contract: QUEUED/SCHEDULED equals "unknown" (`api.py` status mapping). A deploy that is
   scheduled for hours or days does not fit a CI job that waits for the job end. The contract
   with the aac service CI template (outside this repo) is missing in the plan. In addition there is `since_epoch`
   against `start_time` (creation time).
7. The commit author is fixed in the core as "Lyndrix IaC <iac@lyndrix.local>". There is no write path into the
   controller repo, and write permission, branch protection and YAML roundtrip are unresolved.
8. Controller paths in the plan (`hosts.yml`, `stages/<stage>.yml`) do not match
   `environments/sites/<site>/stages/<stage>/{stage_vars,stage,hosts}.yml`.
   `maintenance_default` outside `stage_vars` is not written into the inventory, and stage values
   in `group_vars/all.yml` overwrite each other.
9. `iac:inventory_updated` contains only hosts (no `services`, no service `ci_id`) and only comes from
   runners. The plan expects `kind: host|vm|service`. The plugin only knows `entity_type`
   host|hypervisor (`engine.py:1181`); the container/vm distinction exists only in the
   Server Manager mapping (`engine.py:1250-1255`).
10. Structure: the plugin does not have `app/logic/` yet, it uses `app/controller/`. `PLUGIN_VERSION` is
    maintained twice (`api.py:35`, `entrypoint.py:23`).
11. Non-runner decision: SCHEDULED or QUEUED must be decided on the accepting control-plane instance,
    but only the runner has the data. The proposal is the DB snapshot (pattern
    `publish_repo_snapshots`). The plan does not provide for this.
12. The override end is unknown in advance. Plan "end = job end + buffer" collides with the immediate
    `override_started`. Proposal: `until = now + max_override_minutes` (4 h) and `override_ended`
    on completion. A bus completion event is missing; the emit must be added in `run_pipeline`, orphan and
    abort.
13. Settings: no `settings_schema` in the manifest, no `ctx.get_setting`, no settings actions.
    - There are `SettingField` fields (`controller/settings_schema.py:55-97`, `_build_fields()`
      `:99-209`), around 30 fields in the categories pipeline, GitLab webhooks, Ansible, Terraform and
      repo roles.
    - Storage is in the Vault via `ctx.get_secret`/`set_secret`. Env variables
      `PLUGIN_IAC_ORCHESTRATOR_*` take precedence (`config.py:9-24`).
    - New settings (`override_buffer_minutes`, `max_override_minutes`) belong in `_build_fields()`
      and are rendered automatically by `SchemaField` (`PluginApp.tsx:940`).
    - The default switch is better as its own route plus button than as a field.
14. AI docs: `ai/INSTRUCTIONS.md` (32 lines, English) and the route `/ai/instructions`
    (`api.py:50-58`) exist. They as well as `documentation.md` (sections "API reference" `:99` and
    "Events" `:142`, German) and `CHANGELOG.md` must be extended for scheduled deploys, override and new
    events.
15. Not found: the source code of the service CI (`prod-trigger-orchestrator`) is not in this
    repo. `LYNDRIX_DIRECT_TRIGGER_ENABLED` appears only in the comment `gitlab_webhooks.py:155-158`.
    Renovate does not call the orchestrator directly; the service CI triggers it.
