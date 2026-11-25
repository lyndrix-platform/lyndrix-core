# Sub-plan P2: IaC Orchestrator 1.6.0 — resolve windows, planned deployments, overrides

Decision C (2026-10-18): the orchestrator remains execution. **No default switch with
Git write-back** in this plugin; it only displays the default. Decision H: a `window` deploy without a window in the horizon runs immediately and emits `maintenance:window_missing`.
Decision A: kinds are open (`core.api.maintenance.is_valid_kind`), no fixed list.

Execution: Fable subagent. Basis: `analysis-iac-orchestrator.md`, `00-ergebnis-p0.md`
(sections 2–4). Repo `plugins/lyndrix-plugin-iac-orchestrator`, layout stays
`app/controller/` (no renaming). Prerequisite: core 1.11.0 (API 1.13) runs in the dev core;
registry 0.3.0 knows the scheme `iac.service` (plan-entity-registry.md, roll out beforehand; until
then the registry answers with `ok:false` for service items → `ci_id=None`, no error).

## 1. Manifest and versions

- `entrypoint.py`: `version="1.6.0"`, `min_core_version="1.11.0"`; `app/api.py:35`
  `PLUGIN_VERSION="1.6.0"` (test keeps both equal).
- `permissions.emit` + `maintenance:windows_published`, `maintenance:override_started`,
  `maintenance:override_ended`, `maintenance:window_missing`, `entity_registry:relate`.
- No new permission (the default switch is read-only, `api:read` suffices).
- CHANGELOG, `documentation.md` (DE: extend sections "API-Referenz" and "Events", new
  section "Maintenance windows and planned deployments"), `ai/INSTRUCTIONS.md` (scheduled
  deploys, run now = override, default window display only: change in the controller repo).

## 2. Resolution and publication (`app/controller/maintenance_resolver.py`, new)

Runs only on runners (working trees). Pure functions plus a thin publisher.

```python
def load_tree(inventory_root: Path, site: str, stage: str) -> dict
    # iac_core.app.services.loader.load_configuration(...) — raw merge, values are literal.
    # Additionally sites/<site>/site_vars.yml for hardware_hosts (they get no stage_vars).
def scan_service_ymls(dirs: Iterable[Path]) -> dict[str, ServiceMaint]
    # rglob("service.yml") like engine._load_service_docker_names (engine.py:956-1014); per file
    # try/except with log; reads service.name, maintenance, incidents, stage_overrides[*].maintenance/incidents
def resolve(site, stage, tree, services: dict[str, ServiceMaint], ci: CiMap, *, default_tz) -> list[Item]
    # Item = {ci_id, kind, name, host_ci_id?, windows: [normal form], inherited_from, deploy_policy?, incidents?}
    # chain from 00-ergebnis-p0.md section 2.3; lists replace; tz default = site_vars.timezone or "UTC";
    # validation via core.api.maintenance.parse_windows — invalid blocks: log + ignore block, never abort.
def service_policy(items, service_name, hosts) -> PolicyDecision
    # deploy_policy, windows of the first target host (sorted), note if hosts differ
```

Publisher (`engine.py`, next to `emit_iac_inventory_updated`, lines 1271-1295):

1. Extend `_attach_ci_ids` (1196-1243): items for services (`entity_type="service"`,
   `identifiers={"iac.service": f"{host}/{service}"}`, `display_name=f"{service} @ {host}"`,
   `attributes={"host": host, "site":…, "stage":…}`, `source="iac-orchestrator"`). Result into
   `self._ci_map: dict[tuple, str]` (`("host", name)`, `("service", host, name)`), one
   `ctx.request` with timeout 10 s as today; chunking at > 500 items.
2. Afterwards `entity_registry:relate` per service: `{from_uuid: service_ci, to_uuid: host_ci,
   relation: "runs_on", source: "iac-orchestrator"}` (fire-and-forget).
3. `_build_monitoring_inventory_payload` (1016-1089): `ci_id` on `hosts[]` and `services[]`,
   `host_ci_id` on `services[]`, from `self._ci_map`. **Change order in `NativeGenerateStage`
   (143-153):** first `_attach_ci_ids`, then monitoring sync, then
   `emit_iac_inventory_updated`, then `publish_maintenance()`. At boot
   (`entrypoint.py:302-307`) the same order.
4. `publish_maintenance()`: `resolve` per site/stage → one `ctx.emit(TOPIC_WINDOWS_PUBLISHED,
   {source:"iac-orchestrator", published_at, replaces:true, items})` (local) → snapshot
   `iac_orchestrator_state` key `maintenance_effective` (pattern `publish_repo_snapshots`,
   `api.py:501-522`) for the UI on all instances.
5. `_build_inventory_hosts` (1161-1194): carry `maintenance` and `incidents` from `host_data`
   and `ci_id` as before.

`iac_core/app/validate/models.py:6-12` `ServiceDefinition`: `model_config = ConfigDict(extra="allow")`,
so that `services[].maintenance` survives in the controller (add test).

## 3. Queue: `SCHEDULED`, park, promote, coalesce (`app/controller/queue.py`, `app/model/models.py`)

- Columns (`models.py:11-36`, `queue.py:148-155 wanted`): `not_before DATETIME NULL`,
  `window_ci_ids LONGTEXT NULL` (JSON list), `override_id VARCHAR(64) NULL`,
  `schedule_reason VARCHAR(255) NULL`.
- `STATUS_SCHEDULED = "SCHEDULED"`. Times like the other columns **naive in process clock**
  (`datetime.now()`): helpers `to_queue_clock(aware) -> naive` and `from_queue_clock(naive) -> aware`.
- `claim_next` (224-266): candidates `status='QUEUED' OR (status='SCHEDULED' AND not_before <=
  now)`, sorting `not_before NULLS FIRST, id`; conditional UPDATE on `status IN ('QUEUED','SCHEDULED')`.
- `park(job_id, *, not_before, window_ci_ids, reason) -> bool`: conditional UPDATE
  `RUNNING → SCHEDULED WHERE owner_instance = me`, sets the three fields, clears
  `owner_instance/claimed_at/heartbeat_at`, `progress=0`, `current_step="Scheduled"`.
- `promote(job_id, *, override: bool) -> bool`: `SCHEDULED → QUEUED`, `not_before=NULL`,
  set payload flag `force=true`; caller emits `iac:job_queued` (`service.py:80-90`).
- `enqueue` (184-221): if a `SCHEDULED` job with the same `dedupe_key` exists → **coalesce**:
  replace `payload`, `start_time = now`, return `(job_id, coalesced=True)`; `QUEUED`/`RUNNING`
  remains `DuplicateJob`. Adjust `_conflicts` and `dedupe_key_for` accordingly.
- `abort_queued` (387-406): `SCHEDULED` only with `include_scheduled=True`. `live_summary`
  (457-487): count `scheduled`. `database.py:287-315 clear_all_jobs`: keep `SCHEDULED`.
- `database.py:148-191 get_latest_job_for_pipeline_type`: prefer `SCHEDULED` jobs regardless of the time window (a scheduled job is the current state).
- Orphan sweep: `SCHEDULED` has no owner, is not counted as orphaned.

## 4. Engine: policy stage, park, overrides (`app/controller/engine.py`)

- Webhook payload (`controller/api.py:647-653`): add `trigger: "gitlab-ci"`, `manual`
  remains. The policy applies only to `trigger in ("gitlab-ci", "webhook")`; UI deploys (`trigger="ui"`)
  and `force=true` run immediately.
- `SingleServiceStrategy.build_stages` (660-696): after `CloneServiceRepoStage` a new
  `MaintenancePolicyStage(svc_name, svc_branch, payload)`:
  1. skip if `force` or trigger is not webhook, or job `not_before` set and reached (then only prepare overrides);
  2. `scan_service_ymls([services_dir/svc])` + `load_tree` for the stage of the target hosts
     (hosts from controller `hosts[].services[].name == svc`; stage = controller stage, branch only
     fallback as 666-672) → `service_policy`;
  3. `deploy_policy == "window"` and `current_interval` empty → `next_start` within
     `schedule_horizon_days`; found → `queue.park(...)` and `raise JobParked(not_before)`;
     not found → warning into the job log, continue, and (setting
     `maintenance_report_missing_window`) `ctx.emit(TOPIC_WINDOW_MISSING, {event_id:
     make_window_missing_id(service_ci, job_id), ci_id: service_ci, kind: "service", name,
     host_ci_id, site, stage, horizon_days, job_id, source: "iac-orchestrator", detected_at,
     reason: "deploy_policy window, no window within <n> days"})` per target host service CI (local;
     without service CI no event, only log);
  4. result (`window_ci_ids` = service CIs of target hosts + host CIs) on the context for step 5.
- `run_pipeline` (1346-1660): `except JobParked` before the general handler: log
  `[SYSTEM] Deployment scheduled for <iso> (<reason>)`, `parked=True`; in `finally` with
  `parked` **no** `update_job(final_status)`, release counters/keys as on abort, no `override_ended`. `_skip_claimed` unchanged.
- Overrides (`deploy_marks_maintenance`, setting): directly after the policy stage for
  `single_service` (all triggers): `override_id = f"iac-job-{job_id}"`, `until = now +
  max_override_minutes`, `ctx.emit(TOPIC_OVERRIDE_STARTED, {...linger_minutes})`, `override_id`
  in the job row. `override_ended` in `finally` (1646-1660 and 1718-1723), in
  `queue.mark_failed/fail_orphans` (336-364) and in the abort path (`api.py:1130-1205`,
  `engine.abort_job` 1932) via a service callback `on_job_terminal(job_row)`; emits run
  via `ctx.emit` (thread-safe since core 1.7).
- If `_ci_map` lacks a target host (registry not reachable): no override event, log.

## 5. Default window: display only (decision C)

- No pipeline type, no write stages, no push. The default comes from the snapshot
  `maintenance_effective` (per site/stage `{enabled, windows, source_file}` from
  `stage_vars.maintenance_default` or `site_vars.maintenance_default`).
- Prepare loop protection: `_should_trigger_orchestrator` (`api.py:252-278`) and the
  webhook sync ignore deploy triggers for commits with prefix `ci: maintenance` (sync keeps
  running). This is the prefix the future SSoT manager switch uses
  (`00-ergebnis-p0.md` section 8).
- UI note on the card: "Change in the iac-controller (`stage_vars.yml`, block
  `maintenance_default`)" with the path of the file.

## 6. Settings (`app/controller/settings_schema.py` `_build_fields`, category "Maintenance")

| Key | kind | Default |
|---|---|---|
| `maintenance_deploy_marks_maintenance` | bool | true |
| `maintenance_max_override_minutes` | int | 240 |
| `maintenance_linger_minutes` | int | 15 |
| `maintenance_schedule_horizon_days` | int | 30 |
| `maintenance_report_missing_window` | bool | true (emits `maintenance:window_missing`) |

Access via the existing Vault/env mechanism (`config.py`, env `PLUGIN_IAC_ORCHESTRATOR_*`
takes precedence).

## 7. API (`app/api.py`, handlers in `controller/api.py`)

| Method | Path | Perm | Content |
|---|---|---|---|
| GET | `/schedules` | read | `SCHEDULED` jobs: id, service, branch, `not_before` (ISO), `window_ci_ids`, `schedule_reason`, `start_time` |
| POST | `/schedules/{job_id}/run-now` | write | `promote(override=True)` + `iac:job_queued` (cluster) |
| DELETE | `/schedules/{job_id}` | write | cancel the planned job (`ABORTED`) |
| GET | `/maintenance-windows?site=&stage=&kind=&q=` | read | snapshot `maintenance_effective` plus `effective_state` per item (core library, `now`) |
| GET | `/maintenance-default` | read | per site/stage `{enabled, windows, source_file}` from the snapshot (read-only) |

Public (`webhook_router.py`, `api.py:659-694 do_get_service_status`): query `accept`
(comma list). `SCHEDULED` → with `accept=scheduled`: `{"status":"scheduled","job_status":
"SCHEDULED","not_before":…}`; without: `{"status":"success","job_status":"SCHEDULED","not_before":…}`.
`QUEUED` → `running`. The existing `/maintenance/*` paths remain (plugin maintenance).

## 8. UI (`src/ui/PluginApp.tsx`, `src/ui/lib/api.ts`, `locales/`)

- New tab `maintenance` ("Wartung") in `TabId` (1301), `NavTile`, `ViewHeader`. Three cards:
  **Scheduled deployments** (table, buttons "Run now" with confirm like `abort()`
  1469-1483, "Cancel"), **Effective windows** (filter site/stage/type, badge "active until",
  "next"), **Default windows** (per site/stage state on/off and windows, display only, with
  note on the controller file; no toggle).
- `StatusBadge`/`statusColor` (56-86): `SCHEDULED` → `--lx-state-maintenance` with fallback
  `--lx-state-paused`; `ActivePipelines` shows scheduled separately ("scheduled: n").
- `api.ts`: `listSchedules`, `runNow`, `cancelSchedule`, `maintenanceWindows`,
  `maintenanceDefault`. Locales de/en (i18next dialect).
- NiceGUI pages unchanged (React is the target).

## 9. Tests (`tests/`, in the dev core: `docker cp` + `PYTHONPATH=/app pytest -q tests`)

- `test_maintenance_resolver.py`: inheritance chain (service > controller service > host >
  stage default > site default > none), `inherit: false`, `stage_overrides.prod`, list replaces,
  tz default from `site_vars.timezone`, invalid block is ignored and logged,
  `incident_defaults` per kind, `deploy_policy` default per stage, hypervisor via site.
- `test_queue.py` extend: park/promote, claim takes due `SCHEDULED`, not-due stays,
  coalescing sets `start_time`, `abort_queued` leaves `SCHEDULED`, `clear_all_jobs` keeps `SCHEDULED`.
- `test_policy_stage.py`: decision with fake clock (in window → continue; outside → park with
  `not_before`; no window in horizon → continue with warning **and** exactly one
  `window_missing` emit with deterministic `event_id`, no emit if setting off or without
  service CI; `force` → continue; `trigger=ui` → continue); `JobParked` skips `update_job`.
- `test_inventory_publish.py` extend: service items in `identify`, `relate` emits, `ci_id`
  and `host_ci_id` in monitoring payload, `windows_published` once with `replaces:true`.
- `test_status_mapping.py`: `accept=scheduled` vs. compatibility mode.
- Loop protection: controller commit with prefix `ci: maintenance` triggers no deploy, the
  sync runs.
- `iac_core/tests`: `ServiceDefinition` keeps `maintenance`.

## 10. Acceptance (dev bench)

See `00-ergebnis-p0.md` section 7, steps 2–4, 7 and 8. Additionally: a second webhook for
the same service is coalesced; after a runner restart the `SCHEDULED` job remains and runs when due. `ruff`, `black --check`, `mypy` clean; bundle rebuilt
(`ui_static/ui_bundle.js`). Commit on `feature/maintenance-windows`, tag only after approval
(`v1.6.0` and `1.6.0`).

## 11. Known limits (document)

- A service on several hosts: the window of the first target host (sorted) decides;
  deviating hosts are named in the job log.
- Services the orchestrator has never cloned inherit the host/stage default (no own
  `service.yml` on the runner).
- The default switch is only readable here; on/off is changed in the controller repo until the
  SSoT manager takes over write-back (`00-ergebnis-p0.md` section 8).
- `window_missing` is emitted per deploy job; the deduplication (one open task per CI)
  lies in the ITSM. Without ITSM only the job log warning remains.
