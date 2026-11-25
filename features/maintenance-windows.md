# Maintenance windows, planned deployments and incident rules

Status: 2026-10-17, planning document (phase 0). Author: Lyndrix session with Marvin.
Status: **P0 completed.** The six analyses and the sub-plans are in
`features/maintenance-windows/`. **`00-ergebnis-p0.md` replaces sections 4, 6 and 7 of this
document where they contradict it** (type vocabulary without `vm`, decision in the job instead of at enqueue,
local bus emission, override semantics, ITSM mapping). Next step: review of the
sub-plans with Marvin (open decisions A–H in the result, section 5), then P1.

## 1. Goal

Every monitorable component – hardware (PVE nodes), VMs, containers, services – can have
planned **maintenance windows**. Within a window, monitoring does not raise alarms (state "maintenance" instead of "down", no notifications, no incidents).

Deployments that come from the repository (Renovate → GitLab webhook → IaC Orchestrator)
are **scheduled into the next maintenance window** of the affected service/host. The operator
can override this ("run now"); this **override** is passed to monitoring as a temporary maintenance window
so that the deployment itself does not trigger an alarm.

If a service has no window of its own, a **default maintenance window** applies, which can be
activated in the iac-controller. Windows for hardware, VMs and containers are configured in the iac-controller,
windows for services in the `service.yml` of the service (SSoT).

In addition, there are **incident rules** per service: if service X is offline for longer than Y minutes
outside a (planned or overridden) window (Y from `service.yml`), an incident is created in the ITSM, linked to the host on which the service runs and to the service itself.

## 2. Guiding principles

1. **Each plugin implements its own maintainability.** No plugin *needs* another. Whoever has only the IaC Orchestrator installed gets planned deployments and overrides; the
   events to monitoring then go nowhere (`ctx.emit` is fire-and-forget). Whoever has only monitoring maintains windows
   there directly. Together they form the extension.
2. **The core stays plugin-agnostic.** It only provides a neutral library (window model,
   time evaluation) and the documented bus contract (topics, payloads). No persistence, no
   plugin knowledge in the core.
3. **The single source of truth remains the SSoT.** Windows and incident rules live in the
   iac-controller (hosts, stages) and in the `service.yml` (services). Lyndrix renders,
   displays and evaluates – it is not the second source of truth. What is configured in Lyndrix (overrides,
   manual windows in monitoring, default switch) is runtime state, not a duplicate of the SSoT.
4. **CI identity via the entity registry** (`ci_id`, existing contract): hosts, VMs, services
   are addressed in all events via their `ci_id`, never by IP or name alone.
5. **Degrade instead of break:** if a consumer is missing, a `ci_id` is missing, or a rule is missing → the
   respective plugin behaves as it does today.

## 3. Terms

| Term | Meaning |
|---|---|
| Maintenance window (schedule) | Recurring (e.g. every Sunday 02:00–04:00 Europe/Berlin) or one-time (start/end). |
| Default window | Window that applies to components without their own window; definable per site/stage, activatable in the iac-controller. |
| Inheritance | Service → host → stage/site default → none. A service without its own window inherits that of its host, then the stage default. |
| Override | Temporary window that an operator or a deployment job creates (start = now, end = job end + buffer). Applies to the affected `ci_id`s. |
| Planned deployment | An orchestrator job in state `SCHEDULED` with `not_before` = start of the next window. |
| Incident rule | Per service: "offline ≥ Y minutes outside a window ⇒ incident (severity, auto-resolve)". |

## 4. Configuration sources (SSoT)

### 4.1 `service.yml` (aac service, validated by the template engine)

```yaml
maintenance:
  windows:                       # optional; empty = inherit host/stage default
    - weekly: { days: [sun], start: "02:00", end: "04:00", tz: Europe/Berlin }     # simple format
    - once:   { start: "2026-11-02T22:00+01:00", end: "2026-11-03T02:00+01:00", note: "Storage-Umzug" }
    - cron:   { expr: "0 2 1 * *", duration_minutes: 180, tz: Europe/Berlin }       # extended (like Semaphore: cron + duration)
  deploy_policy: window          # window | immediate; default: prod → window, dev/test → immediate
  override_buffer_minutes: 15    # override window = job duration + buffer
incidents:                       # overrides the global type defaults (controller) for this service
  enabled: true
  offline_minutes: 10            # Y; 0 = no incident rule
  severity: high                 # ITSM severity
  auto_resolve: true             # close incident when the service is back online
```

### 4.2 iac-controller (hosts, hardware, VMs, stages)

```yaml
# hosts.yml / hardware / VM definitions
maintenance:
  windows: [...]                 # as above
  inherit: true                  # services on this host inherit if they have nothing of their own
# stages/<stage>.yml (or site)
maintenance_default:
  enabled: true                  # the "default window" switch (also switchable from the orchestrator UI → Git commit)
  windows:
    - weekly: { days: [sun], start: "02:00", end: "04:00", tz: Europe/Berlin }
incident_defaults:               # global rules per CI type (types = entity registry kinds)
  service: { enabled: true,  offline_minutes: 10, severity: high,   auto_resolve: true }
  host:    { enabled: true,  offline_minutes: 5,  severity: critical, auto_resolve: true }
  vm:      { enabled: true,  offline_minutes: 5,  severity: high,   auto_resolve: true }
  container: { enabled: false }
```

Resolution of the incident rule: service/host entry in the SSoT → `incident_defaults[<type>]` of the stage →
none. In addition, monitoring has a **runtime emergency switch per type** (plugin setting) that
stops the creation of outage events without a repo change – not a second rule set, just an off switch.

The rendering (template engine / `iac_core` in the orchestrator) resolves the inheritance and delivers the **effective**
windows and rules per host/service into the inventory variables; the orchestrator publishes them (section 6).

Decided: the default switch is operated from the orchestrator UI and written back to the iac-controller via Git commit
(one truth, convenient). The orchestrator needs write access to the controller repo for this (existing Git path
of the plugin, commit author "Lyndrix IaC Orchestrator").

## 5. Core: library and contract (API 1.13)

`core.api.maintenance` – pure functions, no persistence:

- `Schedule` (weekly | once | cron+duration), `MaintenanceWindow` (ci_id, source, schedule, note, override:bool);
  cron evaluation via `croniter` (or own minimal implementation) with time zone
- `parse_windows(raw: list[dict]) -> list[Schedule]` (validates the YAML schema from 4.x)
- `active_until(schedules, now) -> datetime | None` (running window), `next_start(schedules, after)`
- Topic constants and payload schemas of the bus contract (section 6), so that all plugins use the same
  names; documentation in `docs/plugins.md` ("Maintenance (API 1.13)").

No new core endpoints except possibly `GET /api/maintenance/contract` (documentation only) – deliberately
minimal, the core knows no windows.

## 6. Bus contract (all optional, fire-and-forget)

| Topic | Emitter | Payload | Consumers |
|---|---|---|---|
| `maintenance:windows_published` | Orchestrator (after render/sync), monitoring (manual windows) | `{source, items:[{ci_id, kind: host|vm|service, windows:[Schedule], inherited_from?}], replaces: true}` | Monitoring, Registry, Server Manager, ITSM |
| `maintenance:override_started` | Orchestrator (deploy now), monitoring (manual), ITSM (change) | `{override_id, ci_ids[], until, reason, source}` | Monitoring, Registry |
| `maintenance:override_ended` | ditto | `{override_id}` | ditto |
| `monitoring:outage_persisted` | Monitoring | `{ci_id, host_ci_id?, service?, since, duration_s, rule:{offline_minutes, severity, auto_resolve}}` | ITSM |
| `monitoring:outage_recovered` | Monitoring | `{ci_id, since, recovered_at}` | ITSM (auto-resolve) |

Rules: `ci_id` mandatory (emitter resolves it beforehand via the registry, otherwise no event); consumers
ignore unknown fields; each consumer keeps its own state (table) and manages without the emitter. Topics are declared in the manifests under `permissions.emit/subscribe`.

## 7. Work per plugin

### 7.1 IaC Orchestrator (`lyndrix-plugin-iac-orchestrator`)
- SSoT parsing: read `maintenance`/`incidents` from `service.yml` and controller hosts/stages,
  resolve inheritance, publish per CI (`maintenance:windows_published`, after each sync).
- Job queue: new states `SCHEDULED` (+ `not_before`, `window_ci_ids`); webhook deploys with
  `deploy_policy: window` are not immediately `QUEUED` but `SCHEDULED` for the start of the next
  window; the worker picks them up when due. Multiple deploys of the same service in the same window are combined (latest state wins).
- UI: list of planned deployments with window, button "Run now" → override: job immediately
  `QUEUED`, `maintenance:override_started` for host+service CIs (end = job end + buffer),
  `override_ended` on completion.
- Default window switch: display of the effective default per stage; activation depending on question 1.
- Guardrails: without monitoring no change in behavior except the planning; without `ci_id` no event.

### 7.2 Monitoring (`lyndrix-plugin-monitoring`)
- Own table `monitoring_maintenance` (ci_id/monitor_id, schedule JSON, source, note) and
  `monitoring_overrides` (override_id, ci_ids, until, reason); consumer of the bus events, additionally
  **manual windows in its own UI** (per monitor/host, one-time or recurring).
- Evaluation on probe result: if a window/override is active for the monitor (via `ci_id`) →
  state `maintenance` instead of `down`, no notification (Discord/notification router), no transition
  event as alarm; dashboard shows "maintenance until hh:mm".
- Incident rule: per monitor the rule from the bus (`rule`) or locally configured; if a monitor runs
  outside a window for ≥ `offline_minutes` → `monitoring:outage_persisted` once;
  on return `monitoring:outage_recovered`. State in a small table, cluster-safe
  (only the prober lease instance evaluates).

### 7.3 ITSM (`lyndrix-plugin-itsm`)
- Consumer: `monitoring:outage_persisted` → create incident (dedupe: open incident per `ci_id`),
  severity from the rule, CI links to service and host CI, description with duration/monitor.
- `monitoring:outage_recovered` → auto-resolve (if the rule allows it), otherwise comment.
- Own maintenance knowledge: a planned change can publish a window as override
  (`maintenance:override_started`) – second stage, not part of the first release.

### 7.4 Entity Registry, Server Manager, Docker Manager, External Services
- Registry: attribute `maintenance` on the CI (current/next window, source) from the bus events –
  read model, so every UI can show "in maintenance" (`GET /entities/{id}` extended).
- Server Manager: display of the effective window and running overrides on the host.
- Docker Manager / External Services: display badge, optionally own manual overrides (later).

### 7.5 IaC workspace (collaboration with the IaC session)
- Template engine: schema for `maintenance`/`incidents` in `service.yml`, validation, rendering
  of the effective values into the inventory variables.
- iac-controller: schema for hosts/stages (`maintenance`, `maintenance_default`), documentation.

### 7.6 Shell (lyndrix-ui)
- Nothing central needed; plugins show their own badges. Optionally later: maintenance badge in the
  plugin list from the registry read model.

## 8. Phases

| Phase | Content | Who |
|---|---|---|
| P0 | Analysis sweeps per plugin (cheap subagents): current data models, events, UI locations, test status, gaps against section 7 | Sonnet/Haiku agents, briefings in section 10 |
| P1 | Core library + contract + documentation (API 1.13), tests of the time logic (time zones, month change, daylight saving time) | Fable |
| P2 | Orchestrator: parsing, `SCHEDULED`, override, UI | Fable subagent with sub-plan |
| P3 | Monitoring: tables, suppression, manual windows, outage rule | Fable subagent with sub-plan |
| P4 | ITSM: incident from outage, auto-resolve, CI links | Fable subagent with sub-plan |
| P5 | Registry/Server Manager read model and display | Fable subagent with sub-plan |
| P6 | IaC: template-engine and controller schema, documentation; handover to the IaC session | IaC session |
| P7 | Dev-bench prototype: Renovate deploy → SCHEDULED → override → monitoring "maintenance" → incident rule outside window → ITSM | Fable |

Each phase ends with tests (pure functions + plugin suites in the dev core) and a release tag;
rollout order as usual (core → plugins → k3s roll by Marvin).

## 9. Decisions (Marvin, 2026-10-17)

1. **Default window switch:** definition in the iac-controller, on/off also from the orchestrator UI
   with write-back via Git commit (one truth).
2. **Default deploy behavior:** `prod` waits for the next window, `dev`/`test` deploy immediately;
   overridable per service via `deploy_policy`.
3. **Window format:** both – simple schema (`weekly`, `once`) as standard, extended
   `cron` + `duration_minutes` (like Semaphore).
4. **Incidents:** auto-resolve on return, rules for services **and** hosts/VMs; **global
   rules per CI type** (types from the entity registry) as default, overridable per service in the `service.yml`
   or per host in the controller; runtime emergency switch per type in monitoring.
5. Assumptions without query: multiple deploys of the same service in the same window → latest state
   wins; override buffer 15 minutes, maximum override duration 4 h (both as setting).

**Addendum 2026-10-18 (answers to A–H from `maintenance-windows/00-ergebnis-p0.md` §5):**
Point 1 is refined: the orchestrator remains execution and only displays the default switch; the write-back via Git commit goes into a separate project in the SSoT manager plugin
(C). Types are all entity registry kinds, open for future ones such as `network` (A). If no window
appears on the horizon, the orchestrator deploys immediately with a warning and the ITSM creates a
task "Define maintenance window" (H, topic `maintenance:window_missing`). B, D, E, F, G
with the proposed defaults.

## 11. Result P0 (2026-10-17)

| File | Content |
|---|---|
| `maintenance-windows/00-ergebnis-p0.md` | Findings, corrected contract (schema, payloads, transport rule), architecture decisions, open decisions A–H, phases/versions, P7 prototype |
| `maintenance-windows/analysis-{iac-orchestrator,monitoring,itsm,entity-registry,server-manager,template-engine-controller}.md` | Analysis sweeps (Sonnet), exact to file:line |
| `maintenance-windows/plan-core.md` | P1: `core.api.maintenance`, API 1.13, Core 1.11.0, Shell 1.8.0 |
| `maintenance-windows/plan-iac-orchestrator.md` | P2: resolver, service CIs, `SCHEDULED`/park, overrides, default switch, 1.6.0 |
| `maintenance-windows/plan-monitoring.md` | P3: `ci_id`, `MAINTENANCE`, windows/overrides, outage rule, 1.7.0 |
| `maintenance-windows/plan-itsm.md` | P4: incidents from outage events, auto-resolve, 0.3.0 |
| `maintenance-windows/plan-entity-registry.md` | P5a: read model, scheme `iac.service`, 0.3.0 |
| `maintenance-windows/plan-server-manager.md` | P5b: badge/filter, 1.4.0 |
| `maintenance-windows/plan-iac-workspace.md` | P6: linter, controller documentation/examples, CI status contract (handover to IaC session) |

## 10. Briefings for the analysis sweeps (P0)

Each sweep delivers a Markdown file `features/maintenance-windows/analysis-<plugin>.md` with:
data model (tables/columns, `ci_id` usage), relevant events (emit/subscribe), API routes,
UI components with file:line, test status/recipe, cluster specifics (lease, shard), and
a list of concrete hook points for section 7 – without changing any code.

- Orchestrator: webhook path (GitLab/Renovate) to the job, queue states, worker, where `service.yml`
  and controller data are read, `iac:inventory_updated` payload, UI job list.
- Monitoring: monitor model, probe loop, transition/notification path, `ci_id` on the monitor,
  inventory sync from the orchestrator, dashboard components, prober lease.
- ITSM: incident model, creation via API/bus, CI links, dedupe options, catalog YAML.
- Entity Registry: entity attributes, extensibility, `lookup`/`identify` contracts.
- Server Manager: host model, display locations.
- Template engine / iac-controller: `service.yml` schema validation, render path, stage overrides.
