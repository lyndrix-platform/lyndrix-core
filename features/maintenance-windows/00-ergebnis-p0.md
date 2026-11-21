# P0 result: findings, corrected contract, decisions

Status 2026-10-17, decisions A–H from Marvin on 2026-10-18 incorporated (section 5).
Synthesis of the six analysis sweeps (`analysis-*.md` in this folder) by the
Lyndrix session (Fable). This document replaces sections 4, 6 and 7 of the main plan
`../maintenance-windows.md` where it contradicts them. The sub-plans `plan-*.md` build on it.

## 1. Findings that change the plan

| # | Finding | Source | Consequence |
|---|---|---|---|
| 1 | The entity registry knows the kinds `host`, `hypervisor`, `service`, `container`, `network`, `itsm_record`, `generic`. **There is no `vm`.** VMs and LXCs are `host`, physical nodes `hypervisor`. | registry §2.2 | Type vocabulary of the feature = **the kinds of the entity registry, open** (today `hypervisor`, `host`, `service`, `container`, `network`, `itsm_record`, `generic`; more in future). No fixed list in the code, only a slug check. `incident_defaults` accepts every kind as key. Sources in the first release: controller and `service.yml` for `hypervisor`/`host`/`service` (decision A). |
| 2 | **Nobody creates `service` entities**, and the relation `runs_on` is maintained by no plugin. The orchestrator only identifies hosts and hypervisors. | registry §2.2, §2.5; orchestrator §2 | The orchestrator will identify services in future (`entity_type: service`, new scheme `iac.service = <host>/<service.name>`) and set `runs_on` service → host. Events additionally carry `host_ci_id`, so that consumers do not depend on the relation. |
| 3 | **The monitoring has no `ci_id`** on the monitor, uses no registry and receives its inventory via HTTP POST from the orchestrator (`/inventory-sync`), not via the bus. | monitoring §1, §4 | The orchestrator enriches the inventory-sync payload with `ci_id` (hosts and services) and `host_ci_id` (services); the monitoring stores `ci_id` on the monitor. Order in `NativeGenerateStage`: first `_attach_ci_ids`, then monitoring sync. |
| 4 | **The monitoring has no notification path.** `monitoring:state_changed` has no consumer. | monitoring §1, §10.2 | "No alarms within the window" concretely means: state `maintenance` instead of `down`, no outage events, and every later alarm path must check `in_maintenance`. No Discord hook to build. |
| 5 | **The orchestrator does not read `service.yml`**; `iac_core` reads only the controller. The template engine sees `service.yml` only at deploy time on the runner. | orchestrator §8.1, §9.1; TE §5 | The resolution of the effective windows happens in the orchestrator on the runner (variant A): controller tree via `iac_core.loader` plus `services_dir/*/service.yml`. The template engine only validates (linter) and passes through. |
| 6 | The decision "immediately or scheduled" today happens at enqueue on an arbitrary control-plane instance, which has **no Git working trees**. | orchestrator §8.2.5 | **Decision in the job ("park")**: the runner claims the job, clones the service repo (existing first stage), evaluates the policy and parks the job as `SCHEDULED` with `not_before`. Always fresh, no snapshot needed for the decision. |
| 7 | The service CI (`prod-trigger-orchestrator`) polls up to 1800 s for `success`/`failed`; `QUEUED` and a future `SCHEDULED` become `unknown` → the CI job turns red. | TE §8; orchestrator §3.6 | The status endpoint delivers `scheduled` + `not_before` only if the client sends `?accept=scheduled` (new CI template, P6). Old templates continue to get `success` with `job_status: SCHEDULED` (compatibility mode). |
| 8 | Duplicate deploys: today **the first wins** (`DuplicateJob`), not the latest state. | orchestrator §7 | Only for `SCHEDULED` jobs are they coalesced (update payload and `start_time`). For `QUEUED`/`RUNNING` today's behavior remains. |
| 9 | The orchestrator has **no write path into the controller repo**; the role `iac_controller` is configured read-only, the commit author in the core is fixed to "Lyndrix IaC". `stage_vars.yml` contains a TF backend block with a credential key name. | orchestrator §8.4 | **The orchestrator does not write into the controller** (decision C: it remains execution). It only reads `maintenance_default` and displays state and windows. On/off is edited in the first release in the controller repo (`stage_vars.yml`, one line); write-back from the UI is a follow-up project in the SSoT manager plugin (section 8). |
| 10 | The end of an override is unknown in advance; there is no bus completion event for jobs. | orchestrator §9.12 | `override_started` with `until = now + max_override_minutes` (default 240), `override_ended` in `run_pipeline` finally, orphan sweep and abort. Consumers let the override run on after `ended` for `linger_minutes` (default 15). |
| 11 | ITSM incidents have **no severity**, but priority P1–P4 from `impact × urgency`. "Open" is not a catalog filter (`closed_at IS NULL` includes `resolved`). `on_hold → resolved` is forbidden. | itsm §3, §9.3–9.5 | Mapping table `critical→1/1`, `high→1/2`, `medium→2/2`, `low→3/2`. Dedupe via state kind `open`. Auto-resolve from `on_hold` falls back to a comment. New catalog code `auto_recovered`. |
| 12 | Every instance runs all bus handlers; no consumer has single-writer protection today. | itsm §8, registry §8, server-manager §7 | Transport rule in section 3: local emission, idempotent handlers, DB as state. ITSM uses `UNIQUE(source, external_id)` with deterministic `external_id = outage_id`. |
| 13 | Controller layout is `environments/sites/<site>/stages/<stage>/{stage,stage_vars,hosts}.yml`; only keys **under `stage_vars:`** become host vars; hardware hosts get no stage_vars. Lists are merged in the loader, replaced in the template engine. | TE §4, §6, §9 | `maintenance_default` and `incident_defaults` sit under `stage_vars:` (stage) respectively `site_vars:` (site, applies to hardware). Window lists are defined per level **only on one level** (inheritance replaces, never mixes). |
| 14 | No time zone/cron library in the core, in the plugins or in the TE image; `zoneinfo` is available everywhere. | own check; TE §10.8 | Own 5-field cron parser in `core.api.maintenance`, no new dependency. The template engine linter checks syntax only (stdlib). |
| 15 | Several plugins still use the layout `app/controller/` instead of `app/logic/` (orchestrator, monitoring, server manager). | all sweeps | Place new modules in the **existing** structure. The renaming is a separate project, not part of this feature. |

## 2. Type vocabulary and schema (corrected)

Types = entity registry kinds, **open** (decision A: all, in future also `network` etc.).
The code checks only the slug (`^[a-z][a-z0-9_]*$`), not membership in a list.
Sources in the first release: `hypervisor` (physical nodes, controller `hardware.yml`), `host` (VMs
and LXCs, controller `hosts.yml`), `service` (aac service from `service.yml`, one
instance per host). `container`, `network` and all other kinds get windows today only via
manual windows in monitoring or future emitters; consumers treat them equally.

### 2.1 Window format (normalized, this is also how it appears on the bus)

```yaml
# Input (service.yml / controller)               # normal form (bus, tables)
- weekly: {days: [sun], start: "02:00", end: "04:00", tz: Europe/Berlin}
                                                 # {"type":"weekly","days":["sun"],"start":"02:00","end":"04:00","tz":"Europe/Berlin"}
- once:   {start: "2026-11-02T22:00+01:00", end: "2026-11-03T02:00+01:00", note: "Storage-Umzug"}
                                                 # {"type":"once","start":"…","end":"…","note":"…"}
- cron:   {expr: "0 2 1 * *", duration_minutes: 180, tz: Europe/Berlin}
                                                 # {"type":"cron","expr":"0 2 1 * *","duration_minutes":180,"tz":"Europe/Berlin"}
```

Rules: `weekly.end < start` means "across midnight". `tz` is optional; if missing, the
site time zone applies (`site_vars.timezone`, present in the controller), otherwise UTC. `days` accepts
`mon…sun` and `0…6` (0 = Monday, ISO). Cron: 5 fields, `*`, lists, ranges, steps, month and weekday
names, `0`/`7` = Sunday, no `@` macros.

### 2.2 `service.yml`

```yaml
maintenance:
  windows: [...]                 # empty/missing = inherit host → stage default → site default → none
  deploy_policy: window          # window | immediate; missing = prod → window, otherwise immediate
incidents:                       # missing = incident_defaults.service of the stage
  enabled: true
  offline_minutes: 10            # 0 = no rule
  severity: high                 # critical | high | medium | low
  auto_resolve: true
stage_overrides:
  prod:
    maintenance: {...}           # replaces the whole block for this stage (lists are replaced)
```

### 2.3 iac-controller

```yaml
# sites/<site>/stages/<stage>/hosts.yml           # per host (VM/LXC), next to terraform:/services:
hosts:
  docker-hydra:
    maintenance:
      windows: [...]
      inherit: true              # services on this host inherit if they have nothing of their own
    incidents: {...}             # optional, host rule
    services:
      - name: aac-grafana
        config: {}
        maintenance: {...}       # optional: windows of this service instance on this host
                                 # (ServiceDefinition needs extra="allow", see plan-iac-orchestrator)

# sites/<site>/hardware.yml                       # per hypervisor
hardware_hosts:
  hydra:
    maintenance: {windows: [...]}

# sites/<site>/stages/<stage>/stage_vars.yml      # UNDER stage_vars:
stage_vars:
  maintenance_default:
    enabled: true                # the default switch; edited in the repo (write-back from the UI: SSoT manager, section 8)
    windows: [...]
  incident_defaults:             # keys = arbitrary registry kinds
    service:    {enabled: true,  offline_minutes: 10, severity: high,     auto_resolve: true}
    host:       {enabled: true,  offline_minutes: 5,  severity: critical, auto_resolve: true}
    hypervisor: {enabled: true,  offline_minutes: 5,  severity: critical, auto_resolve: true}
    container:  {enabled: false}
    network:    {enabled: false} # example for a kind without source in the first release

# sites/<site>/site_vars.yml                      # UNDER site_vars: (applies to hardware_hosts, which see no stage_vars)
site_vars:
  timezone: Europe/Berlin        # already exists
  maintenance_default: {...}
  incident_defaults: {...}
```

Resolution (specific wins, lists are replaced):

- Windows service: `service.yml` (+ `stage_overrides[stage]`) → controller `services[].maintenance`
  → host `maintenance` (only if `inherit` is not `false`) → stage `maintenance_default` (if
  `enabled`) → site `maintenance_default` → none.
- Windows host/hypervisor: own `maintenance` → stage default (host) respectively site default
  (hypervisor) → none.
- Incident rule: own block → `incident_defaults[<kind>]` of the stage → of the site → off.
- `deploy_policy`: service → stage default (`prod` → `window`, otherwise `immediate`).

The stage of a service is the **controller stage of the target host**, not the branch.

## 3. Bus contract (final, payloads)

Transport rule: `maintenance:*` and `monitoring:outage_*` are emitted **locally** (no
`cluster=True`). Reason: plugins with `placement=any` run on every instance; the local
consumer of the emitter writes into the shared DB, so exactly one instance processes each
event. Consumers with in-memory cache (monitoring) invalidate their peers with their **own**
cluster broadcast. Handlers are nevertheless idempotent (unique keys, delete+insert per `source` in
one transaction), so that a later switch to `cluster=True` is possible without harm.
Timestamps on the bus are ISO-8601 with offset (UTC preferred). Topics are listed in
`permissions.emit` respectively `permissions.subscribe` of the manifests.

| Topic | Emitter | Payload |
|---|---|---|
| `maintenance:windows_published` | orchestrator (runner, after boot and each `NativeGenerateStage`), monitoring (manual windows, `source: "monitoring"`) | `{source, published_at, replaces: true, items: [{ci_id, kind, name, host_ci_id?, windows: [normal form…], inherited_from: null \| "host" \| "stage_default" \| "site_default", deploy_policy?: "window" \| "immediate", incidents?: {enabled, offline_minutes, severity, auto_resolve}}]}` |
| `maintenance:override_started` | orchestrator (every executed deploy job, "Run now"), monitoring (manual override), later ITSM | `{override_id, ci_ids: [uuid…], until, reason, source, started_at, linger_minutes: 15}` |
| `maintenance:override_ended` | ditto | `{override_id, source, ended_at, linger_minutes: 15}` |
| `monitoring:outage_persisted` | monitoring (probing instance, once per outage) | `{outage_id, ci_id, kind, name, host_ci_id?, monitor_id, since, persisted_at, duration_s, rule: {offline_minutes, severity, auto_resolve}, error?}` |
| `monitoring:outage_recovered` | monitoring | `{outage_id, ci_id, since, recovered_at, duration_s}` |
| `maintenance:window_missing` | orchestrator (runner, when a `deploy_policy: window` deploy runs immediately because no window lies in the horizon; decision H) | `{event_id, ci_id, kind: "service", name, host_ci_id?, site, stage, horizon_days, job_id, source: "iac-orchestrator", detected_at, reason}` |

`outage_id` is deterministically `"<ci_id>:<since as ISO-UTC>"`; ITSM uses it as
`external_id`. `event_id` is deterministically `"window_missing:<ci_id>:<job_id>"`; ITSM creates
from it a **task** ("Define maintenance window") and deduplicates per event and per open
task on the CI. `replaces: true` means: the consumer replaces **all** entries of this `source` in
one transaction. An item without `ci_id` is not sent by the emitter. A consumer ignores
unknown fields and unknown `ci_id`s.

Override semantics at the consumer: active from `started_at` to `until`; after
`override_ended` still until `ended_at + linger_minutes`. Expired overrides are ignored on read
and occasionally deleted; no timer needed.

## 4. Architecture decisions (Fable, for review)

1. **Decision in the job ("park")** instead of a snapshot on the control plane (finding 6). Flow:
   webhook → `QUEUED` as today → runner claims → `CloneServiceRepoStage` → new
   `MaintenancePolicyStage` → with `window` and not within the window: `park(job, not_before =
   next window start, window_ci_ids)` → status `SCHEDULED`, claim released, no
   final status. Due: `claim_next` takes `SCHEDULED` with `not_before <= now` → fresh clone
   (fresh `service.yml`) → policy stage sees `not_before` reached → runs. No window in
   30 days → warning, execute immediately (deploys must not hang forever) **and**
   emit `maintenance:window_missing`; ITSM creates from it a task with the job to define a
   window (decision H).
2. **Every executed deploy is an override** (`deploy_marks_maintenance`, default on): not
   only "Run now", also `immediate` deploys and catalog button. Reason: every deploy
   restarts containers and would otherwise produce `down`. "Run now" = promote SCHEDULED → QUEUED;
   the override then arises during the run.
3. **Service CIs and `runs_on`** come from the orchestrator (finding 2). Identifier
   `iac.service = "<host>/<service.name>"` (extend registry catalog, priority 20),
   `display_name = "<service> @ <host>"`. `host_ci_id` additionally travels in every event.
4. **`ci_id` into monitoring via the existing HTTP inventory sync** (finding 3), not via a
   further bus subscription. `config_hash` includes `ci_id`.
5. **Outage clock:** `down_since` is set in the same commit as the heartbeat; if a window
   or override runs, `suppressed_until = window end` is advanced; effective outage time =
   `now - max(down_since, suppressed_until)`. One-time delivery via conditional `UPDATE … WHERE
   state='open'`. Only the probing instance evaluates (lease or `owns()` in shard mode).
6. **Monitoring state `MAINTENANCE`** replaces only `DOWN` (a successful probe stays `UP`).
   Own aggregate column `maintenance_samples`; uptime denominator = `total - paused - maintenance`.
   Precedence for groups: `DOWN > UNKNOWN > MAINTENANCE > UP > PAUSED`. New color token
   `--lx-state-maintenance` (core whitelist + shell + default theme), fallback `--lx-state-paused`.
7. **ITSM dedupe** via `source="monitoring"`, `external_id=outage_id` (cluster-safe through
   `UNIQUE`), plus query "open incident with `affected` link to `ci_id` and
   `source=monitoring`" → on a hit a comment instead of a new record. A renewed outage after
   `resolved` creates a new incident (no reopen). Host CI as `related`. Auto-resolve with
   `resolution_code: auto_recovered` (catalog), mandatory notes generated. Notification
   `record_created` for automatically created P1/P2.
8. **Read models** in registry and server manager as **own tables**, evaluation
   (current/next window, expired overrides) **on read** via the core library,
   no per-entity events on mass publish.
9. **Default switch read-only in the orchestrator** (decision C). The orchestrator shows per
   site/stage `enabled` and `windows` from the controller snapshot; changed in the
   controller repo. Loop protection nevertheless already reserves the commit prefix
   `ci: maintenance` (no deploy trigger), so that the future SSoT manager commit triggers no
   deploy. Write-back from the UI moves into the SSoT manager plugin (section 8).
10. **CI status contract** (finding 7): `GET …/gitlab/status?accept=scheduled` → `{"status":
    "scheduled", "not_before": …}`; without `accept` → `status: "success"`, `job_status: "SCHEDULED"`.
    When coalescing a `SCHEDULED` job `start_time = now` is set, so that the second CI finds the job via `since_epoch`.

## 5. Decisions (Marvin, 2026-10-18)

| # | Question | Decision |
|---|---|---|
| A | Type vocabulary? | **All registry kinds, open** (in future also `network` etc.). No `vm`. Code checks only the slug. |
| B | Every executed deploy creates an override (not only "Run now")? | yes (default accepted), setting `deploy_marks_maintenance` |
| C | Push right for the orchestrator on `iac-controller/main`? | **No.** The orchestrator remains execution and only reads. Write-back of the default switch becomes its own project in the SSoT manager plugin (section 8). Until then: edit line `enabled:` in the controller repo. |
| D | CI template conversion (`?accept=scheduled`) by the IaC session in P6; until then compatibility mode? | yes (default accepted) |
| E | Server manager: only badge + filter, detail view later? | yes (default accepted) |
| F | ITSM: new incident per outage, no reopen of a `resolved` one? | yes (default accepted) |
| G | Time zone without `tz`: site `timezone`, otherwise UTC? | yes (default accepted) |
| H | No window in 30 days? | Deploy immediately with warning (setting `schedule_horizon_days`) **and** create an ITSM task "Define maintenance window" for the service (topic `maintenance:window_missing`, section 3). |

## 6. Phases, dependencies, versions

| Phase | Sub-plan | Version | Dependency |
|---|---|---|---|
| P1 | `plan-core.md`: `core.api.maintenance`, API 1.13, token, docs — **implemented 2026-10-18** (core `50d94f5`, ui `fca418d`) | core 1.11.0, ui 1.8.0 | none |
| P2 | `plan-iac-orchestrator.md` | orchestrator 1.6.0 | P1; registry scheme `iac.service` from P5a (order: roll out P5a before P2) |
| P3 | `plan-monitoring.md` | monitoring 1.7.0 | P1; `ci_id` in the inventory sync from P2 |
| P4 | `plan-itsm.md` | itsm 0.3.0 | P3 payloads (fixed in the contract, buildable in parallel) |
| P5a | `plan-entity-registry.md` | entity-registry 0.3.0 | P1 (guard) |
| P5b | `plan-server-manager.md` | server-manager 1.4.0 | P1 (guard) |
| P6 | `plan-iac-workspace.md` (handover to the IaC session) | template-engine 0.0.10, controller docs | P2 status contract |
| P7 | prototype (section 7) | – | all |

Build: P1 first (Fable itself), then P2–P5 in parallel as subagents with one sub-plan each,
rollout order core → registry → orchestrator → monitoring → ITSM → server manager → k3s roll.
Each sub-plan ends with tests in the dev core, CHANGELOG, docs (DE), version + tag, no push until
approval.

## 7. P7 prototype (dev bench)

1. Controller fixture: stage `dev` with `maintenance_default` (window now+2 min to +10 min),
   host with own window, `incident_defaults`. Service fixture `service.yml` with
   `deploy_policy: window` and `incidents.offline_minutes: 1`.
2. Webhook `POST /api/iac/deploy/service/<svc>/gitlab` → job `QUEUED` → runner clones → `SCHEDULED`
   with `not_before`; status endpoint delivers `success`/`SCHEDULED` respectively `scheduled` with
   `?accept=scheduled`; second webhook is coalesced.
3. "Run now" in the UI → job runs → `override_started` → monitoring shows the monitors of the
   host/service as `maintenance`, registry and server manager show the badge.
4. Job end → `override_ended` → after `linger_minutes` normal again.
5. Stop service (outside window) → after `offline_minutes` `outage_persisted` → ITSM incident
   with CI links service (`affected`) and host (`related`), priority from severity; start service →
   `outage_recovered` → incident `resolved` (`auto_recovered`).
6. The same within the window → no incident, state `maintenance`.
7. Default switch: flip line `enabled:` in `stage_vars.yml` of the controller fixture
   repo, commit → sync → windows disappear/appear in monitoring; the
   orchestrator card "Default windows" shows the new state (read-only).
8. Service with `deploy_policy: window` without window in the horizon (set horizon to 1 day,
   remove window) → webhook → job runs immediately, job log warning, `window_missing` → ITSM
   shows a task "Define maintenance window: <service>" with CI links; second deploy →
   work note on the open task, no second task.

## 8. Follow-up projects (not part of this feature)

- **SSoT manager: default switch with Git write-back** (decision C). The plugin
  `plugins/lyndrix-plugin-aac-ssot-manager` gets the switch per site/stage (targeted line
  change `enabled:` in block `maintenance_default` under `stage_vars:`, commit prefix
  `ci: maintenance default <site>/<stage> on|off`, then sync webhook → orchestrator
  republishes). Prerequisites: own Git credential with push right on
  `iac-controller/main`, plugin repo on GitHub (missing today, memory
  `lyndrix-ssot-manager-repo-missing`). The orchestrator already reserves the commit prefix.
- **Further kinds as window source** (`network`, `container` …): emitters that send
  `maintenance:windows_published` with their own `source` (e.g. Docker Manager for
  containers). Consumers need no change for this.
- **Server manager detail view** for windows/overrides (decision E).
- **Service CI template** `?accept=scheduled` (P6, IaC session); afterwards retire the
  compatibility mode in the status endpoint.

## 9. Review of the subagent reports (Fable, 2026-10-18)

Status: P4 ITSM 0.3.0 (`4878594`), P5a entity registry 0.3.0 (`255708d`), P5b server manager
1.4.0 (`4bb5df7`) committed, local. P2 and P3 were interrupted by the session limit and continue
running (working trees with WIP). Answers to the open questions of the agents:

| Plugin | Question | Decision |
|---|---|---|
| all consumers | `override_id` in window rows `NOT NULL DEFAULT ""` instead of NULL | accepted (NULL does not collide in the unique index; only so does the key catch duplicate deliveries) |
| all consumers | `override_ended` matches `override_id` + `source`, if `source` is sent along | accepted; the contract guarantees the same `source` |
| all consumers | repeated `override_started` after `override_ended` does not reactivate | accepted (idempotency before order) |
| server manager | window rows store only `windows` + `inherited_from` | accepted for decision E; detail view later needs `kind`, `name`, `host_ci_id`, `deploy_policy`, `incidents` (migration, section 8) |
| server manager, registry | windows of several sources are merged on read | meant that way: "specific wins" is resolved by the emitter within its `source`, sources add up |
| ITSM | event loss if ITSM is disabled or tables are missing for > 30 s | accepted for the first release; follow-up: monitoring re-emits open outages periodically (e.g. every 30 min, ITSM deduplicates via `outage_id`) |
| ITSM | notification `link` is not rendered by the shell | follow-up core/shell: generic link in notifications; ITSM payload stays as built |
| ITSM | task `due` only as catalog field, core column `due_at` stays NULL | accepted; set both with the SLA phase |
| ITSM | `task.yml` now only knows the category `maintenance` | accepted and documented; further categories come into the catalog when needed |
| ITSM | one open monitoring incident per CI, independent of the monitor | meant that way (decision F) |
| ITSM | times in titles/notes in UTC | accepted; later `tz` in the event, once monitoring knows the site time zone |
| ITSM | own setting `auto_text_locale` (de/en) for generated texts | accepted (bus events have no user locale) |
| registry | `entity_registry:maintenance_changed` local or `cluster=True`? | **`cluster=True`**: it is a UI refresh signal without write consequences, peers must see it (the own cluster broadcast from the transport rule) |
| registry | `host_ci_id` from `windows_published` is discarded, `/host` uses only `runs_on` | accepted; the orchestrator sets `runs_on` at publish. Fallback column only if the prototype shows a gap |
| registry | invalid windows are skipped, counted, logged | accepted |
| registry | rows on decommissioned entities remain | accepted |
| registry, ITSM, server manager | repos are not black-formatted; agents did not reformat existing files | correct; a pure style commit per repo is Marvin's decision |

Additions to section 8 (follow-ups): monitoring re-emit of open outages; notification link
in core/shell; server manager migration for the detail view; `tz` in outage events.

### 9.1 P2 IaC orchestrator 1.6.0 (`09c020e`)

75 plugin tests green (+42), `iac_core/tests` 11 green and 1 **pre-existing** failure
(`test_terraform_gen.py::test_providers_exclude_secrets`, also red on the baseline). Deviations
accepted: own slim `load_tree` instead of `iac_core.loader` (same merge semantics, testable
on temp directories); `gitlab_pipeline_success` counts among the policy triggers; `enqueue()`
remains `int`, new `enqueue_ex()`; a UI deploy or `force` **promotes** a parked job
instead of leaving it; one `windows_published` for all sites/stages; snapshot keeps items
without `ci_id` (UI shows ⚠), the bus does not; `iac:inventory_updated` additionally receives `services[]`.

| Question | Decision |
|---|---|
| `not_before` naive in the process clock like the other job columns | accepted: exactly one runner (`pinned:runner`), containers run in UTC. Follow-up queue columns to UTC |
| registry 0.2.0 rejects service items → no service `ci_id`, no `window_missing` | known, hence rollout order registry before orchestrator |
| bell notification on `window_missing` without ITSM? | no (would duplicate the ITSM task); job log warning suffices |
| compatibility mode turns old CI jobs green although parked | decision D; P6 switches the templates to `?accept=scheduled` |
| pre-existing `iac_core` test failure and package name collision `tests`/`iac_core/tests` | follow-up in the repo |

### 9.2 P3 monitoring 1.7.0 (`1e1edc2`)

95 tests green (+45, all clocks frozen). Deviations accepted: `monitors_changed` with
`reason=maintenance_windows` (the existing `maintenance` means "housekeeping, reload probe jobs");
additional column `host_ci_id`; manual windows as one row per target
`(monitoring, ci_id, monitor_id)`; `PassiveResult` with `MAINTENANCE` → 422 instead of 400
(Pydantic validator); settings categories "Incidents" and "Maintenance windows" (the category
"Maintenance" carries the housekeeping buttons); service without own `ci_id` borrows that of the host and
counts as kind `service`. All new routes (`/windows`, `/overrides`, `/outages`) are secured with
`api:read`/`api:write`, foreign sources return 409 (security review checked).

| Question | Decision |
|---|---|
| window/override on the host CI does not implicitly cover service monitors with own `ci_id` | accepted: the orchestrator resolves the inheritance and sends service **and** host CIs in `window_ci_ids`; the dialog posts per monitor of the host. Observe in P7 |
| `GET /windows` `effective` only with CI overrides | sufficient |
| outage under borrowed host `ci_id`: kind `service`, rule from the host | ok, ITSM uses only the severity |
| `outage_recovered` is emitted by the instance that stores the heartbeat | ok (conditional UPDATE = exactly once, local consumer) |
| inventory-sync keys `ci_id`/`host_ci_id` | orchestrator 1.6.0 sends exactly these keys |

**Follow-up project (new):** a host outage today creates one incident per service plus one
for the host. Correlation host → services (only host incident, services as `related`) belongs in
a later wave (monitoring knows `host_ci_id`, ITSM can suppress).

### 9.3 Dev bench after the rework (2026-10-18, before P7)

- **Reconcile loop:** the mounted plugins carried new manifest versions, `plugin_states` the
  old ones → the cluster reconcile reloaded the old tags, uvicorn restarted every few seconds
  (3098 reloads in 3 h). Fixed via `~/.config/lyndrix/dev-align-plugin-states.sh` (Marvin, auto mode
  blocks DB update and container `rm`). Lesson: after a version bump of mounted
  plugins align the `plugin_states` rows **immediately** (memory `lyndrix-dev-bench-testing`).
- **Monitoring 1.7.0:** during the loop ~50 000 `Unknown column 'maintenance_samples'` on
  `dev-b` — the `ALTER … IF NOT EXISTS` in `_ensure_schema` swallow errors silently
  (`except: pass`), in the storm they did not get through; since the quiet bench zero errors, column
  present. During the prod rollout check the monitoring logs for this message after the roll.
- **Orchestrator boot publish (bug, fixed in `62b5893`):** after the core restart the registry still reported
  `database not ready`; the orchestrator published "0 item(s) with ci_id (108
  resolved)" and with `replaces: true` deleted the windows of all consumers. Now: repeat the boot chain
  with backoff (15/30/60/120/300 s), hold back `windows_published` as long as the
  registry has not answered; still update the snapshot for the UI. 7 new tests.
- Theme `marvin` (not versioned) warns on every load "missing component style
  BADGE_STATE_MAINTENANCE / STATUS_RAIL_MAINTENANCE" — the validator fills it in, the warning is
  just noise; Marvin can add the two keys to `components.json`.
