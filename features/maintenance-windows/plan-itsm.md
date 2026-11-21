# Sub-plan P4: ITSM 0.3.0 — incidents from outage events, auto-resolve, task when a window is missing

Execution: Fable subagent. Basis: `analysis-itsm.md`, `00-ergebnis-p0.md` (section 3
payloads, section 4 point 7, section 5 decision H). Repo `plugins/lyndrix-plugin-itsm`
(canonical layout present). No dependency on the core library; `min_core_version`
stays `1.4.0`. Can be built in parallel to P2/P3, because the payloads are fixed in the contract.

## 1. Manifest and catalog

- `entrypoint.py:76` `version="0.3.0"`. `permissions.subscribe` (110-125) +
  `monitoring:outage_persisted`, `monitoring:outage_recovered`, `maintenance:window_missing`,
  `itsm:record_created` (own broadcast, for the new notification).
- `catalog/record_types/incident.yml`: extend `resolution_code` (96-104) by `auto_recovered`;
  declare fields `monitor_rule` (string) and `outage_id` (string) as custom fields so that
  the catalog validates them (`service.py:400`). Category `monitoring` (40) and subtype `outage`
  (15) exist.
- `catalog/record_types/task.yml`: custom field `source_ref` (string, origin ID like
  `event_id`) and category `maintenance` (list `categories` is empty today; check whether an
  empty list means "everything allowed" or "nothing", and add accordingly).
- CHANGELOG, `documentation.md` (DE: new section "Automatic incidents from monitoring" and
  "Tasks from maintenance events", events table), locales de/en (texts for journal and notification).

## 2. Logic (`app/logic/monitoring_incidents.py`, new; `entrypoint.py` stays thin)

```python
SEVERITY_TO_IMPACT_URGENCY = {"critical": (1, 1), "high": (1, 2), "medium": (2, 2), "low": (3, 2)}
SOURCE = "monitoring"
AUTHOR = "monitoring"

class MonitoringIncidents:
    def __init__(self, service: ItsmService, ctx): ...
    async def handle_outage_persisted(self, payload: dict) -> str | None   # returns record uuid
    async def handle_outage_recovered(self, payload: dict) -> None
```

`handle_outage_persisted`:

1. Check mandatory fields `outage_id`, `ci_id` (36 characters), `rule`; otherwise log + `itsm:ingest_failed`.
2. **Dedupe 1 (outage):** `service.find_by_external(SOURCE, outage_id)` → exists → do nothing
   (event delivered twice).
3. **Dedupe 2 (CI):** `service.find_open_for_ci(ci_id, record_type="incident", source=SOURCE)`
   — join `Record × CiLink` (`entity_uuid == ci_id`, `role == "affected"`), `Record.state` in the
   states with kind `open` per catalog (`new`, `in_progress`, `on_hold`; **not**
   `closed_at IS NULL`, see analysis-itsm §3). Hit → `add_journal(ref, "work_note",
   "Repeated/further outage reported: <name>, since <since>, duration <duration>", AUTHOR)`,
   leave `custom_fields.outage_id` unchanged, done.
4. Otherwise `create_record(data, author=AUTHOR)` with `record_type="incident"`,
   `subtype="outage"`, `category="monitoring"`, `impact/urgency` from
   `SEVERITY_TO_IMPACT_URGENCY[rule.severity]` (unknown → `high`), `title =
   f"{name}: unreachable since {since_local}"`, `description` (monitor, CI, host, rule,
   duration, last error), `tags=["auto", "monitoring", kind]`, `custom_fields={"monitor_rule":
   json, "outage_id": outage_id, "ci_kind": kind}`, `source=SOURCE`, `external_id=outage_id`,
   `ci_links=[{"entity_uuid": ci_id, "role": "affected"}] + ([{"entity_uuid": host_ci_id, "role":
   "related"}] if host_ci_id)`. CI links via `resolve_ci_links` (registry name cache,
   fallback raw UUID link if the registry fails). `IntegrityError` on
   `uq_itsm_source_ext` (instance race) → treat as success (read the record of the winner).
5. `itsm:record_created` is emitted by the service; the new notification (section 4)
   takes effect there.

`handle_outage_recovered`:

1. `find_by_external(SOURCE, outage_id)` → missing → log, done.
2. State `resolved`/`closed`/`cancelled` → nothing.
3. `rule.auto_resolve` from `custom_fields.monitor_rule` (not from the payload, which does not know the
   rule) → `True`: `transition(ref, "resolved", fields={"resolution_code":
   "auto_recovered", "resolution_notes": f"Automatically resolved: monitor reachable again at
   {recovered_at}, outage duration {duration}."}, note=..., author=AUTHOR)`.
   `TransitionError` (e.g. from `on_hold`) → fallback comment. `VersionConflict` → re-read once
   and retry, then give up with log.
4. `auto_resolve == False` → `add_journal(ref, "comment", "Monitor reachable again …", AUTHOR)`
   (triggers `record_commented`).

Service additions (`app/logic/service.py`): `find_by_external(source, external_id)`,
`find_open_for_ci(ci_id, *, record_type, source=None)`, `open_state_names(record_type)` from
the catalog (`catalog.py` knows state kinds). Everything synchronous, called via `asyncio.to_thread`
like the other handlers.

### 2b. Task "Define maintenance window" (`app/logic/maintenance_tasks.py`, new; decision H)

```python
SOURCE = "maintenance"
AUTHOR = "maintenance"

class MaintenanceTasks:
    async def handle_window_missing(self, payload: dict) -> str | None
```

Payload (contract section 3): `event_id, ci_id, kind, name, host_ci_id?, site, stage,
horizon_days, job_id, source, detected_at, reason`.

1. Check mandatory fields `event_id`, `ci_id` (36 characters), `name`; otherwise log + `itsm:ingest_failed`.
2. **Dedupe 1 (event):** `find_by_external(SOURCE, event_id)` → exists → nothing (delivered twice).
3. **Dedupe 2 (CI):** `find_open_for_ci(ci_id, record_type="task", source=SOURCE)` (states
   with kind `open`: `open`, `in_progress`) → hit → `add_journal(ref, "work_note",
   "Renewed deploy without maintenance window: job <job_id> at <detected_at> (<site>/<stage>)",
   AUTHOR)`, done. **No reopen** of a `done`/`cancelled` one (as with F); the next deploy without
   window then creates a new task.
4. Otherwise `create_record(record_type="task", title=f"Define maintenance window: {name}",
   description=…, impact=3, urgency=2  # P4
   , category="maintenance", assignment_group="infra", tags=["auto", "maintenance", kind],
   custom_fields={"source_ref": event_id, "due": detected_at + 14 days},
   source=SOURCE, external_id=event_id,
   ci_links=[{entity_uuid: ci_id, role: "affected"}] + ([{entity_uuid: host_ci_id, role:
   "related"}] if host_ci_id), author=AUTHOR)`. Description (DE): the service has
   `deploy_policy: window`, no window lay within the horizon of `horizon_days` days, the deploy
   `job_id` therefore ran immediately; instructions: set a window in `service.yml` (`maintenance.windows`)
   or in the controller (`hosts.yml` host window / `stage_vars.maintenance_default`);
   reference to the docs page. `IntegrityError` on `uq_itsm_source_ext` → read the record of the
   winner, treat as success.
5. No notification (P4); the task appears in the list with tag `auto`.

## 3. Bus handler (`entrypoint.py`, pattern `_on_record_create` 261-277)

```python
@ctx.subscribe("monitoring:outage_persisted")
async def _on_outage_persisted(payload): await incidents.handle_outage_persisted(payload)
@ctx.subscribe("monitoring:outage_recovered")
async def _on_outage_recovered(payload): await incidents.handle_outage_recovered(payload)
@ctx.subscribe("maintenance:window_missing")
async def _on_window_missing(payload): await tasks.handle_window_missing(payload)
```

Errors → `itsm:ingest_failed` (existing `_fail`), never an exception to the outside. No lease:
idempotency comes from `UNIQUE(source, external_id)` (deterministic `outage_id`) and the
CI query; a simultaneous duplicate across instances is anyway the exception thanks to the local transport rule.

## 4. Notification for automatically created incidents

Extend `_notify` (`entrypoint.py:362-370`) by endpoint `record_created`
(`notification_endpoints` 142-149); subscriber to `itsm:record_created`: only if
`record.source == "monitoring"` **and** priority P1/P2 → severity `warning`, text
"Incident <number> created automatically: <title>", link `/apps/lyndrix-plugin-itsm/itsm?record=<uuid>`.
Manually created records remain as today (no message on creation).

## 5. UI (optional, small)

`RecordList` (`PluginApp.tsx:275`): make the tag chip "auto" visible and filter "Automatic
(monitoring)" via `tag=auto`. Detail: custom fields `outage_id`/`monitor_rule` are rendered by the
catalog logic generically. Rebuild the bundle.

## 6. Tests (extend `tests/test_service.py` or new `tests/test_monitoring_incidents.py`)

Fixture `svc` + `_RegistryCtx` (`test_service.py:25-44`, `:341-370`):

- Persisted → incident with P2 at `high`, links `affected` (service) and `related` (host),
  `source/external_id`, tags, custom fields.
- Persisted twice (same `outage_id`) → one record.
- Second persisted with different `outage_id`, open incident at the CI → work note instead of record.
- Recovered with `auto_resolve: true` → `resolved`, code `auto_recovered`, `resolved_at` set.
- Recovered from `on_hold` → comment, state remains.
- Recovered with `auto_resolve: false` → comment `comment`.
- Recovered for unknown `outage_id` → nothing, no error.
- Renewed outage after `resolved` → new incident.
- Unknown severity → `high`; invalid `ci_id` → `ingest_failed`.
- Registry not reachable (`ctx.request` → `None`) → record with raw UUID links.
- `window_missing` → task P4 with title, tags, `source_ref`, `due`, links `affected`/`related`;
  delivered twice → one task; second event (different `event_id`) with open task → work note;
  after `done` → new task; invalid payload → `ingest_failed`.

Run: `docker cp . lyndrix-core-dev:/tmp/itsm && docker exec -w /tmp/itsm -e PYTHONPATH=/app:/tmp/pytest-sm
lyndrix-core-dev python -m pytest -q tests`; `ruff check entrypoint.py app tests`.

## 7. Acceptance

`00-ergebnis-p0.md` section 7, steps 5–6 and 8 on the dev bench with real events.
Commit on `feature/maintenance-windows`, tags `v0.3.0`/`0.3.0` after approval.
