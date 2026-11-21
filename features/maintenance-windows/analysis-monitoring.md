# Analysis of the monitoring plugin for maintenance windows (plan section 7.2)

State 2026-10-17, read-only P0 sweep (Sonnet). The sweep returned the text as a report; this
file is the unchanged archive copy. Paths relative to `plugins/lyndrix-plugin-monitoring` unless
stated otherwise. The backend lives under `app/controller/`, not `app/logic/`. The rename is
noted as a TODO (`entrypoint.py:25-28`).

## 1. Overview

| Item | Value | Evidence |
|---|---|---|
| Manifest ID | `lyndrix.plugin.state_monitoring` | `entrypoint.py:47` |
| Version | 1.6.2 | `entrypoint.py:49`, `CHANGELOG.md:8` |
| `min_core_version` | 1.6.0 | `entrypoint.py:54-55` |
| `placement` | not set (default `any`); prober exclusivity runs via lease code in the service | `entrypoint.py:46-167` |
| `auto_enable_on_install` | False | `entrypoint.py:56` |
| UI | `react_ui=True`, route `/monitoring`, plus a NiceGUI page | `entrypoint.py:60-68`, `:369-376` |
| subscribe | `db:connected`, `vault:ready_for_data`, `monitoring:config_upsert`, `monitoring:passive_result`, `monitoring:admin_override`, `monitoring:monitors_changed`, `monitoring:quiesce`, `plugin:settings_changed`, `cluster:changed` | `entrypoint.py:112-126` |
| emit | `monitoring:state_changed`, `monitoring:monitors_changed`, `monitoring:quiesce` | `entrypoint.py:127` |
| Roles | viewer (api:read), operator and admin (read+write); admin auto-map `INT_ADMIN` | `entrypoint.py:138-166` |
| Core API | `__api_version__ = "1.12.0"` | `lyndrix-core/app/core/api/__init__.py:149` |

Important findings as they stand:

- **No `ci_id` in the plugin.** There is no column, no field and no `entity_registry:*` call.
  `ctx.request` is not used.
- **No notification in the plugin.** There is no `notification_service`, no `messaging:outbound`
  and no `system:notify`.
  - The only side effect of a state change is `monitoring:state_changed`
    (`app/controller/service.py:1467`).
  - There is no consumer for it in the workspace. Only `lyndrix-core/app/core/bus.py:27,48` summarises it
    for the log; `events_api.py` and `bridge.py` do not contain it.
  - `documentation.md:185-215` (`monitoring:status_changed`, `system:notify`,
    bus `monitoring:inventory_sync`) is outdated.
- **Inventory arrives via an HTTP POST from the orchestrator, not via the bus.** Details in 4.

## 2. Data model

All tables use `extend_existing`, have no foreign keys and no `relationship()`.

| Table | Columns | Evidence |
|---|---|---|
| `monitoring_monitors` | PK `monitor_id` String(191); `name`; `monitor_type` (server/service/http/ping); `owner_source` (default `ui_admin`); `target`; `address`; `host_name`; `service_name`; `logical_group`; `interval_seconds`; `timeout_seconds`; `enabled`; `source_hash`; `config_hash`; `metadata_json`; `latest_state` String(16); `latest_latency_ms`; `latest_error`; `last_checked_at`; `is_admin_overridden`; `uptime_24h/7d/30d`; `created_at`; `updated_at` | `app/model/models.py:26-56` |
| `monitoring_heartbeats` | `id`, `monitor_id` (idx), `state`, `latency_ms`, `error_message`, `source`, `created_at` (idx); composite index `(monitor_id, created_at)` from `_ensure_schema` | `models.py:59-69`, `service.py:726-744` |
| `monitoring_daily_aggregates` | `monitor_id`, `day`, `total/up/down/paused_samples`, `avg_latency_ms`, `latency_samples`; unique `(monitor_id, day)` | `models.py:72-93` |

- There are no further tables. The uptime windows are columns on the monitor, recomputed
  hourly (`service.py:1184-1226`).
- **States:** `UP`, `DOWN`, `PAUSED`, `UNKNOWN` (`models.py:12-16`), no `MAINTENANCE`.
  - `PAUSED` only comes from an admin pause or `enabled=False` (`service.py:1670-1671`, `:1713-1717`).
  - `aggregate_state` (DOWN > UNKNOWN > UP > PAUSED) is in `app/ui/styles.py:78-90`. An
    unknown state falls back to `UNKNOWN` there.
- **Identity:** `monitor_id` follows the convention `host:<host_name>` or `service:<host>:<service>`
  (`service.py:2164`, `:2195`; `_host_key_for` `:2060-2067`).
- **Host, service, container:**
  - Host = `server`, ICMP with TCP fallback (`:601-612`).
  - Service = `http` or `service` (`:2192`).
  - Container probe only if `metadata.deploy_type == "docker_compose"` plus `address` and
    `service_name` (`:614-616`).
  - The service→host relationship runs only via `host_name` and the ID prefix.
- **Tags/groups:** no tag table. There is `logical_group` (site/stage, `:2171`, `:2203`) and
  `metadata_json.groups` (`:2175`, `:2209`).
- **Ownership:** `owner_source` `orchestrator_service` or `iac_orchestrator` counts as "managed"
  (`:1850`). Upserting a foreign-owned monitor raises `ValueError` (409), except with `force`
  (`:1649-1650`).

## 3. Probe loop and transition/notification path

Flow, only on the prober instance:

1. `setup()` calls `queue_bootstrap()` and `init_placement()` (`entrypoint.py:311-313`).
   `init_placement` loads the settings and then calls `claim_prober_lease()` (`service.py:401-405`).
2. Lease obtained → `start_probing()` (`:231-243`) → `start()` (housekeeping jobs, `:184-200`) and
   `queue_bootstrap()`.
   - `_bootstrap_async` (`:833-850`) creates the tables under the advisory lock `lyndrix_bootstrap`
     (`:715-724`), syncs the scheduler jobs, does `refresh_rollups` and starts the sweep.
3. Each monitor has one interval job `monitor:<id>` (`scheduler.py:58-87`, created in
   `_sync_job_on_loop` `service.py:1534-1569`). PAUSED or disabled means: no job (`:1548-1556`).
4. `run_scheduled_probe` (`:2069-2149`):
   - Skip when in flight, quiesced or not owned (`:2081`).
   - Phase 1: `_load_probe_record` (`:566-590`).
   - Circuit breaker per host, threshold 8 (`:2095-2098`, `:114`).
   - Phase 2: `_dispatch_probe` (`:592-626`).
   - Flap damping: DOWN is only written after 3 failures, and only if the old state was
     UP/None (`:2128-2134`).
   - Phase 3: `_save_probe_result` in the thread (`:2138`). Errors 1020/1205/1213 are discarded
     (`:2139-2147`).
5. `_save_probe_result` (`:628-646`) uses `_rc_session()` (READ COMMITTED, `:123-135`), calls
   `_store_heartbeat` and commits.
6. **`_store_heartbeat`** (`:1478-1506`):
   - sets `latest_state/latency/error/last_checked_at/updated_at`
   - creates the heartbeat
   - calls `_bump_daily_aggregate` (`:1073-1110`, MySQL upsert `:1120`)
   - calls `_emit_transition`
7. **`_emit_transition`** (`:1462-1476`):
   - no emit if the state does not change
   - otherwise `_view_cache.clear()` and `self._emit("monitoring:state_changed", {monitor_id,
     previous_state, new_state, timestamp, error_message})`
   - the emit happens before the commit

Other callers of `_store_heartbeat` or `_emit_transition` run on every instance, also on
standby:

- `ingest_passive_result` (bus `monitoring:passive_result` and `POST /passive`;
  `entrypoint.py:355-360`, `service.py:1741-1757`)
- `apply_admin_override(pause)` (`:1718`)

Notification: nowhere in the plugin (see 1).

## 4. Events and inventory sync

| Direction | Topic | Payload | Evidence |
|---|---|---|---|
| sub | `db:connected` | `queue_bootstrap` | `entrypoint.py:323` |
| sub | `vault:ready_for_data` | `reload_settings` | `entrypoint.py:335` |
| sub | `plugin:settings_changed` | `{plugin_id}` → `reload_settings` on its own ID | `entrypoint.py:343-346` |
| sub | `cluster:changed` | resync, only in shard mode | `entrypoint.py:339`, `service.py:395` |
| sub/emit | `monitoring:monitors_changed` | `{reason, count, instance_id}`; `reason` ∈ upsert, override, inventory, maintenance, probe_all; peers see `_origin` | `service.py:474-493`, `:436-446` |
| sub/emit | `monitoring:quiesce` | `{on, ttl}` with `cluster=True` | `service.py:499-541` |
| sub | `monitoring:config_upsert` | `MonitorUpsert` | `entrypoint.py:348` |
| sub | `monitoring:passive_result` | `PassiveResult` | `entrypoint.py:355`, `models.py:118` |
| sub | `monitoring:admin_override` | `AdminOverride` | `entrypoint.py:362`, `models.py:127` |
| emit | `monitoring:state_changed` | see 3 | `service.py:1467` |

**Inventory sync (as is):**

- There is no subscription to `iac:inventory_updated`.
- The orchestrator builds the payload in
  `lyndrix-plugin-iac-orchestrator/app/controller/engine.py:1016-1089`. It has
  `owner_source="orchestrator_service"`, `source_revision` (SHA256), `hosts[]` and `services[]`.
- It POSTs it with `LYNDRIX_SYSTEM_API_KEY` to
  `/api/plugins/lyndrix.plugin.state_monitoring/inventory-sync` (`engine.py:1101-1134`). It is triggered
  in `NativeGenerateStage` (`:149`) and at boot (`iac-orchestrator/entrypoint.py:305`).
- Reception: `POST /inventory-sync` → `ingest_inventory` → `ingest_inventory_snapshot`
  (`service.py:1024-1033`, `:2155-2265`).
  - Per host, `host:<name>` (SERVER) is created.
  - Per service, `service:<host>:<name>` is created (HTTP for a URL target, otherwise SERVICE).
  - Vanished monitors of this source are deleted along with heartbeats and aggregates (`:2222-2263`).
  - Afterwards `apply_monitor_set_change("inventory")` follows (`:552`).
- **`ci_id` does not arrive in the monitoring payload.**
  - Only the separate event `iac:inventory_updated` (`{hosts:[…ci_id]}`, `engine.py:1271-1291`)
    carries `ci_id`, and only for hosts (`_attach_ci_ids` `:1196-1241`; identifiers `iac.host`, `pve.vm`,
    `fqdn`).
  - Services have a `ci_id` nowhere.
  - Monitoring does not use this event.
- `queue_inventory_sync` and `_process_inventory_sync_queue` (`service.py:1016-1048`) have no
  caller (dead code).

## 5. API routes

Mounted under `/api/plugins/lyndrix.plugin.state_monitoring/` on every instance. File:
`app/controller/api.py`. R = `plugin:…:api:read`, W = `…:api:write` (`api.py:14-15`).

| Method | Path | Perm | Purpose | Line |
|---|---|---|---|---|
| GET | `/dashboard` | R | `{monitors, stats}`, cached | `api.py:34`, `service.py:1411` |
| GET | `/overview` (`group_by`, `hours` 1..168, `include_paused`, `include_unknown`) | R | grouped and flat view, cached | `api.py:38-66`, `service.py:1385` |
| GET | `/monitors/{id}` | R | one monitor | `api.py:68` |
| GET | `/history/{id}` | R | 24 h heartbeats | `api.py:78` |
| POST | `/monitors` | W | upsert | `api.py:85` |
| POST | `/passive` | W | passive result | `api.py:97` |
| POST | `/admin-override` | W | pause/resume/update | `api.py:107` |
| POST | `/inventory-sync` | W | inventory import | `api.py:117` |
| POST | `/clear-states` | W | reset everything | `api.py:125` |
| POST | `/maintenance/prune`, `/rebuild-stats`, `/reset-stats`, `/probe-all`, `/delete-monitors/{scope}` | W | operational maintenance (settings action buttons) | `api.py:149-235` |

- The namespace `/maintenance/*` belongs to the plugin's operational maintenance, not to the maintenance windows.
  New routes should be called `/windows` and `/overrides`. `/settings` is reserved.
- Cache: `_cached()` with TTL `overview_cache_seconds` (default 10), single-flight, at most 64 keys
  (`service.py:1421-1456`). A transition clears it only locally.

## 6. UI components

**React** (`src/ui/PluginApp.tsx`, 1239 lines; `src/ui/lib/api.ts:96-105` `pluginApi`; poll every
5 s with backoff, `:1106-1175`):

- Type `MonitorState` `:12`. Data shapes: `ServiceView :14`, `Host :27`, `Group :39`, `Row :52`,
  `Stats :69`. There is no `ci_id` and no window field.
- `STATE_COLOR` (`var(--lx-state-up|down|paused|unknown)`) `:140-149`. `STATE_LABEL` and
  `StateBadge` `:320-353`; i18n key `state.*` in `locales/monitoring.de.json:32-37`.
- Components: `ServicesInline :375`, `HostHeader :417`, `HostCard :489`, `HostCardDetail :519`,
  `GroupHeader :562`, `CardsView :616`, `TableView :646`, `SplitView :771` (the only
  detail view, at host level), `Toolbar :873`, `HeaderCard :1051` (counters `:1053-1059`).
- There is no dialog, no form and no monitor management in the React UI. Monitor CRUD
  exists only in NiceGUI (`app/ui/settings.py:11`, `:100-140`).
- Colour tokens:
  - `lyndrix-ui/src/index.css:58-61`
  - whitelist `lyndrix-core/app/core/theming/schema.py:42`
  - `theming/engine.py:275,312`
  - badge classes `lyndrix-ui/src/index.css:453,460` and `lyndrix-core/app/ui/theme.py:148-149,608`
- NiceGUI mirror: `app/ui/page.py:15`, `toolbar.py:39`, `views/{cards:21,table:49,split:29}.py`,
  `_components/host_card.py:110,129`, `group_header.py:13`, `timeline.py`, `widget.py`
  (overall status `:25`), `styles.py:8-90`.
- `.claude/skills/run-state-monitoring/SKILL.md` claims there is no React route. This
  contradicts `entrypoint.py:60-68`.

## 7. Test status

- Files: `tests/test_service.py`, `test_perf_and_status.py`, `test_cluster_lease.py`,
  `test_cluster_readiness.py`, `test_maintenance.py`, `test_sharding.py` (approx. 50 tests).
- Fixtures:
  - `conftest.py:12` imports `core.services` before `core.api`. The meta-path finder
    `_SharedAppFinder` (`:34-62`) makes `monitoring_plugin.app.*` identical to `app.*`.
  - Fixture `svc` in `test_perf_and_status.py:51-62`: in-memory SQLite with `StaticPool`,
    `db_instance` via monkeypatch, `create_all`, `ensure_tables()`.
  - `_FakeCtx` (`:22-45`) only knows `emit(topic, payload)`, `set_status`, `maintenance`, `log`.
  - Helpers `_monitor` (`:65`) and `_beat` (`:73`, backdates heartbeats).
  - `test_sharding.py` has a `_ShardCtx` with `on_lease`, `my_shard`, `is_worker`,
    `get_all_settings`, `roles`, `instance_id`.
- Running: `docker cp <plugin> lyndrix-core-dev:/tmp/t-mon` and then
  `docker exec -w /tmp/t-mon -e PYTHONPATH=/app:/tmp/pytest-sm -e LYNDRIX_CLUSTER_MODE=single
  -e LYNDRIX_COORDINATOR=local lyndrix-core-dev python -m pytest -q tests`.
  - pytest must be installed into `/tmp/pytest-sm` beforehand. It is lost on container recreate
    (docstring `test_perf_and_status.py:1-7`).
- The docstring says "FKs on", but the code has no `PRAGMA foreign_keys=ON`. That is
  irrelevant as long as the plugin stays FK-free.
- Not covered: `ctx.request`, `ctx.get_setting`, time/time zones. The transition logic only has
  `test_transition_emit_from_worker_thread_lands_on_loop` (`test_cluster_readiness.py:85`).

## 8. Cluster specifics

- **Lease:** `ctx.on_lease("monitoring:prober", acquired=start_probing, lost=stop_probing)`
  (`service.py:310`). Without `on_lease` (core < 1.7) probing starts directly (`:287-290`).
  - `probe_roles` filters the candidates (`:265-308`). Shrinking gives the lease up via `release_lease`
    (`:312-334`).
- **Standby:** no scheduler, no sweep, no housekeeping. Only tables (`:837-840`); the API
  keeps running. `health()` reports standby as healthy (`entrypoint.py:228-248`).
- **Shard mode** (`probe_nodes > 1`, core ≥ 1.8):
  - No lease. Every eligible instance probes its share via `ctx.my_shard` (`service.py:357-385`).
  - Global housekeeping runs only on the `is_worker("housekeeping")` worker (`:202-221`).
  - A mode change only applies after a reload (`:419-426`).
  - Consequence: several probers are active at the same time.
- **Status:** `_set_status("starting", …, blocking, retry_after=15)` in the start sweep, afterwards `ready`
  (`:933-969`). `maintenance_window()` (`:971`) wraps inventory import, clear-states, rebuild,
  reset and delete (`api.py:132,162,173,218`).
- **Broadcast:** `_emit_cluster` with `cluster=True`, peers see `_origin` (`:480-493`).
- **DB gotchas already implemented:**
  - `_rc_session` (READ COMMITTED) `:123`
  - `_retrying` on 1020/1205/1213 (`:1857`, `:1865`)
  - batches 20000/200/500 (`:44`, `:1851-1852`)
  - quiesce `:524-541`
  - advisory lock for DDL `:715`
  - MySQL upsert `:1120`
  - heartbeat write discards 1020/1205/1213 `:2139`
- DDL hook: `create_all` in `ensure_tables` (`:708-713`) plus `ALTER … IF NOT EXISTS` in
  `_ensure_schema` (`:726-744`).

## 9. Attachment points for section 7.2

### 9.1 Tables `monitoring_maintenance` and `monitoring_overrides`

- **File:** `app/model/models.py`, next to `:59-93`, `extend_existing`, FK-free.
  - `monitoring_maintenance`: `id, ci_id (idx, null), monitor_id (idx, null), source, schedule_json,
    note, enabled, created_at, updated_at`.
  - `monitoring_overrides`: `override_id PK, ci_ids_json, until, reason, source, created_at, ended_at`.
  - Every row needs `ci_id` or `monitor_id`, as long as 9.2 is open.
- **DDL:** automatically via `create_all` (`service.py:708`), already under the advisory lock. A new
  column on the monitor needs an entry in `_ensure_schema` (`:731`).
- **Logic:** new module `app/controller/maintenance.py` instead of putting even more into `service.py` (2265 lines).
  - `core.api.maintenance` only exists from API 1.13. Until then a `getattr` guard or
    a local mini evaluation is needed; the risk is duplicated logic.
- **Risks:**
  - Time zones. The plugin works in UTC, heartbeats are naive UTC (`models.py:251`,
    `service.py:1195,1787`).
  - No DB read per probe in the hot path. Instead an in-memory cache of the active windows per
    instance, invalidated via bus/cluster broadcast.

### 9.2 `ci_id` on the monitor (prerequisite)

- **Column** `ci_id String(64) null, index` to be created in:
  - `MonitorRecord` (`models.py:26-56`)
  - `MonitorUpsert` (`:100`)
  - `_serialize_monitor` (`:162`)
  - `upsert_monitor` (`service.py:1655-1692`)
  - `_ensure_schema`
- **Pitfall:** `config_hash` (`:1653-1654`) must include `ci_id`, otherwise it is never adopted.
- **Sources:**
  - Variant 1: the orchestrator adds `ci_id` in `_build_monitoring_inventory_payload`
    (`engine.py:1039-1089`). For this, the steps in `NativeGenerateStage` (`engine.py:149-150`) must be
    reordered, because `_attach_ci_ids` today only runs in `emit_iac_inventory_updated`.
  - Variant 2: monitoring subscribes to `iac:inventory_updated` (entry in `permissions.subscribe`)
    and matches via the host name to `host:<name>`.
  - Variant 3: monitoring itself calls `ctx.request("entity_registry:identify"/"lookup")`, with
    degradation on `reply is None`.
- Services have no `ci_id`. The only bridge is `host_name`: a host `ci_id` applies to
  `host:<n>` and all `service:<n>:*`.

### 9.3 Consuming `maintenance:windows_published` / `override_started` / `override_ended`

- **File:** `entrypoint.py`. Three thin `@ctx.subscribe` handlers in the style of `:327-367`, delegating to
  the service via `to_thread`. The topics belong in `permissions.subscribe` (`:112-126`).
- **Persistence** in the DB is mandatory, because standby and prober must see the same data. After
  writing, a broadcast via `_emit_cluster` follows.
  - The branch in `on_monitors_changed` (`service.py:436-446`) today only does `request_resync`. For
    window changes it needs its own "reload cache" path, e.g.
    `reason="maintenance_windows"`.
- `windows_published` with `replaces: true` means: replace all rows of this `source` in one transaction,
  with `_rc_session` and `_retrying`.
- `permissions.emit` (`entrypoint.py:127`) lacks `maintenance:windows_published` (manual windows).
- **Risk:** with `cluster=True` emits the handlers run on every instance, hence double writes. A
  unique key plus treating IntegrityError as "a peer was faster" is needed.

### 9.4 Suppression at transition time (state `maintenance`, no notification)

- **Central point:** `_store_heartbeat` (`service.py:1478-1506`). All write paths run through it:
  active probe `:643`, passive `:1749`. Before `record.latest_state = …` comes an
  `effective = self._effective_state(record, state, now)`. If a window or override is active and the
  state is DOWN, `MAINTENANCE` is stored. `error_message` is kept for diagnosis.
- **Enum** `MonitorState` (`models.py:12`) is extended by `MAINTENANCE`. Consequences:
  - `_bump_daily_aggregate` (`service.py:1098-1110`, `:1120-1156`) has no maintenance column.
    Either add `maintenance_samples` (migration) or count it into `paused_samples`
    (mixes the semantics).
  - Uptime: `_uptime_from_aggregates` uses `total - paused` (`models.py:222-229`). Further places
    that only exclude PAUSED: `_refresh_uptime_windows_sync` (`service.py:1202`) and
    `_calculate_uptime_percentages` (`models.py:201`). `_timeline_uptime_percent` (`:282`) counts only UP.
  - `get_timelines` and `aggregate_state` (`styles.py:78-90`): unknown states become
    UNKNOWN, `MAINTENANCE` must be sorted in there.
  - `status_counts` and `stats` (`service.py:1232-1284`): new field `maintenance_count`.
    `health()` optional.
  - `_sync_job_on_loop` (`:1550`) only removes the job for PAUSED. Probing therefore keeps running during maintenance,
    good for recovery detection.
  - `_load_probe_candidates` (`:894-903`): `MAINTENANCE` would only be "stale"; include it if needed.
  - `PassiveResult.state` (`models.py:121`) would accept `MAINTENANCE` from outside; forbid it if necessary.
- **Flap damping:** the maintenance check may only happen in `_store_heartbeat`, not before.
  Otherwise UP→MAINTENANCE arises without probe evidence.
- **Transition event:** it is not suppressed (cache and consumers need it), but
  gets `maintenance: true` and `ci_id`. The emit sits before the commit (`:1506` vs `:644`); that is
  existing behaviour and yields a phantom event on rollback. Therefore do not use it for the outage logic.
- **Notification:** does not exist today. If an alarm path arises later, `in_maintenance`
  must come before it.
- Open decision: does a successful probe within the window stay `UP`? Recommendation: yes, only DOWN becomes
  MAINTENANCE.

### 9.5 Manual windows in the own UI

- **API:** new W-gated routes in `api.py` after `:123`: `GET/POST/DELETE /windows`,
  `POST /overrides`, `DELETE /overrides/{id}`. Afterwards `_view_cache.clear()` locally plus a
  cluster broadcast.
- **React:**
  - There is no dialog in the code. New files under `src/ui/` with a "Maintenance window" dialog and
    an overrides list.
  - Hook in via buttons in `HostCardDetail :519`, `SplitView :771` and `TableView :646`.
  - The overrides list comes as a section next to `HeaderCard :1051` or into the toolbar menu `:873`.
  - The types `Row`, `ServiceView` and `Host` (`:14-66`) get `maintenance_until?`. i18n keys in
    `locales/monitoring.{de,en}.json`.
- **NiceGUI:** mirror in `app/ui/settings.py` and `_components/host_card.py`, only via the new
  routes (API-first).
- `settings_schema` (`entrypoint.py:71-110`) is suitable for switches and buffers, not for window lists.

### 9.6 Dashboard badge "Maintenance until hh:mm"

- **Backend:** `maintenance_until` in `list_monitors` and `_serialize_monitor` (`service.py:1286`,
  `models.py:162`), computed from the in-memory cache.
  - `build_grouped_overview` and `flatten_monitors` (`helpers.py:147`, `:271-307`) pass fields through via a
    whitelist dict (lines `:290-306`, host/service views `:180-200`). `maintenance_until` must be added
    there.
  - The overview cache (TTL 10 s) delays visibility by up to 10 s; a transition clears it.
- **React:** `StateBadge` (`:327-353`) gets the state `MAINTENANCE` with an optional time. In addition,
  `STATE_COLOR` and `STATE_LABEL` (`:140-149`, `:320-325`) and the
  `HeaderCard` counters (`:1053-1059`) must change.
- **Colour:** a new token `--lx-state-maintenance` is a cross-core change, because the
  core whitelist does not know it. Affected: `lyndrix-ui/src/index.css:58-61`, `schema.py:42`,
  `engine.py:275/312`, `theme.py:148-149,608`. Interim fallback to `--lx-state-paused` or
  `--lx-warning`.
- NiceGUI: `styles.py:8-27`, `widget.py:25`.

### 9.7 Outage rule (offline ≥ Y min outside a window) and emits

- **"Down since" exists nowhere today.**
  - `updated_at` and `last_checked_at` are overwritten on every heartbeat (`service.py:1493-1494`).
  - Heartbeats are deleted after 7 days (`:1003`, `:1782`).
  - Flap damping and circuit breaker create gaps (`:2095-2098`, `:2128-2134`).
  - Reconstruction from heartbeats is therefore unreliable.
- **New table** `monitoring_outages(monitor_id PK, ci_id, down_since, state
  open|persisted|recovered, persisted_at, rule_json, updated_at)`.
  - `down_since` is set in `_store_heartbeat` on the change to DOWN, in the same commit as the
    heartbeat (consistent, no phantom as with the emit).
  - It is closed on return, likewise there and in `apply_admin_override` (`:1703`).
  - `delete_monitors` and `clear_states_db` (`:1929`, `:1803`) must empty the table as well.
- **Evaluation:** periodic interval job (e.g. 60 s, `id="monitoring:outages"`) in `start()`
  (`:184-200`), runs only on the prober.
  - In shard mode only for `owns(monitor_id)` (`:383`).
  - One-time delivery via a conditional `UPDATE … WHERE state='open'` (rowcount 1 = winner). This
    also protects on a lease change and on a race between two instances.
- **Emits:** `monitoring:outage_persisted` `{ci_id, host_ci_id?, service?, since, duration_s, rule}`
  and `monitoring:outage_recovered` `{ci_id, since, recovered_at}` via `self._emit` (`:168`). Both
  belong in `permissions.emit` (`entrypoint.py:127`).
  - Without `ci_id` no event is sent, the monitor is skipped.
- **Rules:** local rule per monitor or the bus `rule`; global defaults per CI type as settings (9.8).

### 9.8 Runtime kill switch per CI type

- **File:** `entrypoint.py` `settings_schema` (`:71-110`). New fields e.g.
  `outage_rules_enabled_host/_vm/_service`, `outage_default_offline_minutes`,
  `override_buffer_minutes` (15), `override_max_hours` (4).
  - The schema so far only uses `int`, `str` and `action`. Whether `kind: bool` exists must be checked in the
    core.
- **Application:** extend `apply_settings` (`service.py:994-1010`). Changes arrive via
  `plugin:settings_changed` without a reload. `ctx.get_setting` is not used in the plugin, only
  `get_all_settings` (`:407`).
- The CI type assignment can be derived from `monitor_type` (`server` = host, otherwise service). VM vs.
  host is only distinguished by `metadata.terraform` (`service.py:2178`) or the `entity_type` from the
  registry.

## 10. Deviations from the plan / open questions

1. **No `ci_id` on the monitor.** The plan presupposes it. The orchestrator does not deliver it to monitoring
   (only Docker Manager and others via `iac:inventory_updated`). Services have a `ci_id` nowhere.
   Decision needed: variant 1, 2 or 3 from 9.2.
2. **No notification path in monitoring.** The plan ("no notification Discord/notification router"),
   `documentation.md:198` and `docs/index.md:8` describe something that is missing in the code.
   `monitoring:state_changed` has no consumer. Alerting would be a feature of its own.
3. **Inventory arrives via HTTP POST with a system API key** (`engine.py:1101-1134`), not via the bus.
   `maintenance:*`, in contrast, arrives via the bus, so two transport patterns in the same plugin.
4. **Emit/subscribe allowlists** (`entrypoint.py:112-127`) contain neither `maintenance:*` nor
   `monitoring:outage_*`.
5. **"Only the lease instance evaluates" holds only in lease mode.** In shard mode several
   instances probe; the evaluation must respect `owns()` and secure delivery via a DB winner.
   Passive results and admin overrides run on every instance, so maintaining `down_since` must
   be DB-atomic.
6. **The semantics "offline ≥ Y min outside a window" is underspecified:** cumulative (subtract window time)
   or restart after the window ends? Flap damping (3 failures, `service.py:113`) and
   interval ≥ 10 s (`models.py:110`) shift `down_since` back by up to approx. 3 intervals.
   The circuit breaker stops probes entirely (`:2095`).
7. **Aggregate and uptime semantics for `maintenance`** are not specified in the plan (denominator, own
   column vs. `paused_samples`, ordering in `aggregate_state`). The token `--lx-state-maintenance`
   is a core and UI change.
8. **`placement`** is not set. `singleton` would be wrong, because the API must run on all
   instances.
9. **`documentation.md`** ("Events", from line 185) is outdated and should be rewritten before 7.2
   (German).
10. **Test gaps:**
    - `_FakeCtx.emit(topic, payload)` does not know `cluster=`. The service catches this via the
      `TypeError` branch `service.py:485-488`, so the `cluster=True` path is not tested.
    - `ctx.request` and clock injection are missing; `_utc_now` is module-global (`models.py:144`),
      replaceable via monkeypatch.
11. **Dead code:** `queue_inventory_sync` / `_process_inventory_sync_queue` (`service.py:1016-1048`)
    are not a template for bus consumption.
12. **Directory:** the plan says `app/logic/`, actually it is `app/controller/`. Place new modules
    there or do the rename beforehand, to avoid a double move.
