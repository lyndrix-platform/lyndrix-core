# Sub-plan P3: Monitoring 1.7.0 — `ci_id`, state `MAINTENANCE`, windows/overrides, outage rule

Execution: Fable subagent. Basis: `analysis-monitoring.md`, `00-ergebnis-p0.md`
(sections 2–4). Repo `plugins/lyndrix-plugin-monitoring`, layout stays `app/controller/`.
Prerequisite: core 1.11.0 (API 1.13); orchestrator 1.6.0 delivers `ci_id`/`host_ci_id` in the
inventory sync (until then `ci_id` stays empty, everything else works).

## 1. Manifest and versions

- `entrypoint.py:49` `version="1.6.2" → "1.7.0"`, `min_core_version="1.11.0"`.
- `permissions.subscribe` + `maintenance:windows_published`, `maintenance:override_started`,
  `maintenance:override_ended`. `permissions.emit` + `maintenance:windows_published`,
  `maintenance:override_started`, `maintenance:override_ended`, `monitoring:outage_persisted`,
  `monitoring:outage_recovered`.
- `settings_schema` (71-110) category "Maintenance" / "Incidents":

| Key | kind | Default |
|---|---|---|
| `outage_rules_enabled` | bool | true (global emergency switch) |
| `outage_rules_enabled_hypervisor` / `_host` / `_service` / `_container` | bool | true / true / true / false (the known kinds as own switches) |
| `outage_rules_disabled_kinds` | str | "" (comma list of further kinds that are silent — kinds are open, decision A; example `network,generic`) |
| `outage_default_offline_minutes` | int | 10 |
| `outage_default_severity` | select critical/high/medium/low | high |
| `outage_default_auto_resolve` | bool | true |
| `override_default_minutes` | int | 60 (manual override from the UI) |
| `override_max_minutes` | int | 240 |
| `override_linger_minutes` | int | 15 |

`apply_settings` (`service.py:994-1010`) takes them over; changes arrive via
`plugin:settings_changed` without reload.

## 2. Data model (`app/model/models.py`, DDL via `create_all` + `_ensure_schema`)

- `MonitorRecord` (26-56): `ci_id String(36) NULL` with index; `MonitorUpsert.ci_id`;
  `_serialize_monitor` returns `ci_id`; `upsert_monitor` (`service.py:1655-1692`) takes it over;
  **`config_hash` (1653-1654) includes `ci_id`**; `_ensure_schema` (726-744) `ALTER … ADD
  COLUMN IF NOT EXISTS ci_id`. Inventory ingest (`ingest_inventory_snapshot` 2155-2265) reads
  `ci_id` from `hosts[]`/`services[]`.
- `MonitorState.MAINTENANCE = "MAINTENANCE"` (12-16). `PassiveResult.state` rejects
  `MAINTENANCE` (400).
- `monitoring_daily_aggregates`: column `maintenance_samples Integer default 0`
  (`_ensure_schema`, MySQL upsert 1120-1156, `_bump_daily_aggregate` 1073-1110). Uptime denominator
  `total - paused - maintenance` in `_uptime_from_aggregates` (`models.py:222-229`),
  `_refresh_uptime_windows_sync` (`service.py:1202`), `_calculate_uptime_percentages`
  (`models.py:201`); `_timeline_uptime_percent` counts `UP` unchanged.
- New tables (all `extend_existing`, FK-free):

```
monitoring_maintenance_windows  id PK, ci_id String(36) idx NULL, monitor_id String(191) idx NULL,
                                source String(64), kind String(16), name String(255),
                                windows_json Text, inherited_from String(32) NULL,
                                incidents_json Text NULL, deploy_policy String(16) NULL,
                                note Text NULL, updated_at; UNIQUE (source, ci_id, monitor_id)
monitoring_overrides            override_id String(64) PK, source String(64), ci_ids_json Text,
                                monitor_ids_json Text NULL, until DateTime, started_at DateTime,
                                ended_at DateTime NULL, linger_minutes Integer, reason Text
monitoring_outages              monitor_id String(191) PK, outage_id String(128) NULL, ci_id String(36) NULL,
                                down_since DateTime, suppressed_until DateTime NULL,
                                state String(16)  -- open | persisted | recovered
                                persisted_at DateTime NULL, recovered_at DateTime NULL,
                                rule_json Text NULL, error Text NULL, updated_at
```

Times naive UTC like the rest of the plugin (`models.py:251`); conversion from/to aware via
helpers in `maintenance.py`.

## 3. Module `app/controller/maintenance.py` (new)

- `MaintenanceStore`: CRUD with `_rc_session` + `_retrying` (1020/1205/1213):
  `replace_windows(source, items)` (DELETE per `source` + INSERT in one transaction),
  `add_manual_window(ci_id|monitor_id, windows, note)`, `delete_window(id)`,
  `upsert_override(...)`, `end_override(override_id, ended_at, linger)`, `list_*`,
  `purge_expired(now)` (overrides with `until + linger < now - 1 d`), `open_outages()`.
- `MaintenanceCache` (per instance, in memory, no DB read in the probe hot path):
  `reload()` loads windows (`parse_windows`), overrides (`Override`), rules per `ci_id`, and the
  mapping `monitor_id → ci_id` (monitor column, fallback: host monitor `host:<host_name>`
  for services without own `ci_id`); `state_for(record, now) -> MaintenanceState` via
  `core.api.maintenance.effective_state`; `rule_for(record) -> IncidentRule | None`
  (order: rule on the service CI → rule on the host CI → settings default of the kind;
  kind from `windows` table, otherwise `server` monitor → `host`, otherwise `service`); apply the emergency switch
  here (`outage_rules_enabled*` for the known kinds, `outage_rules_disabled_kinds` for all
  others; unknown kind without entry → rule applies). No kind gate: every
  slug from the event is accepted (`core.api.maintenance.is_valid_kind`).
- Bus handlers (thin in `entrypoint.py` following pattern 327-367, work in the module via `to_thread`):
  `on_windows_published` (only `source != "monitoring"`; `replaces` → `replace_windows`),
  `on_override_started`, `on_override_ended`. After each write: `cache.reload()` locally and
  `_emit_cluster("monitoring:monitors_changed", reason="maintenance")`; `on_monitors_changed`
  (`service.py:436-446`) gets the branch `reason == "maintenance" → cache.reload()` (no resync). Handlers are idempotent (unique keys, upsert per `override_id`).
- Manual windows/overrides from the UI: write, then **locally** `ctx.emit` of
  `maintenance:windows_published {source:"monitoring", replaces:true, items}` respectively
  `override_started/ended {source:"monitoring"}`, so that the registry and server manager see them;
  own handlers ignore `source == "monitoring"` (otherwise double writes).

## 4. Suppression in the write path (`service.py:1478-1506 _store_heartbeat`)

All heartbeat paths (active 643, passive 1749) run through here. Order:

1. `ms = cache.state_for(record, now)`; `effective = MAINTENANCE if state == DOWN and ms.active
   else state`. `UP` stays `UP`. `latest_error` is kept for diagnostics.
2. Heartbeat with `effective`, `_bump_daily_aggregate` counts `maintenance_samples`.
3. **Outage bookkeeping in the same session** (consistent with the commit, no phantoms):
   - `effective in (DOWN, MAINTENANCE)` and no row → `INSERT monitoring_outages(state="open",
     down_since=now, suppressed_until = ms.until if ms.active else NULL)`.
   - existing open row and `ms.active` → `suppressed_until = max(suppressed_until, ms.until)`.
   - `effective == UP` and row present: `state == "open"` → delete row (never reported);
     `state == "persisted"` → `state="recovered"`, `recovered_at=now`, event
     `outage_recovered` to be emitted **after the commit** (event list on the session, emit in the
     caller after `commit()`), delete row afterwards.
   - `apply_admin_override(pause)` (1703) and `delete_monitors`/`clear_states_db` (1929, 1803)
     clear `monitoring_outages` as well.
4. `_emit_transition` (1462-1476): extend payload by `ci_id` and `maintenance: bool`; still before the commit (existing behavior, only for cache/UI).

Flap damping (2128-2134) stays before this point; the maintenance check happens only here.

## 5. Outage evaluation (interval job `monitoring:outages`, 60 s)

- Registered in `start()` (184-200), runs only on the probing instance; in shard mode for
  `owns(monitor_id)` (383) and additionally the `housekeeping` worker for foreign leftovers
  after a shard change.
- Per open row (`state == "open"`): `rule = cache.rule_for(record)`; `None`/disabled →
  continue. `ms = cache.state_for(record, now)`; active → `suppressed_until = max(…, ms.until)`,
  continue. Otherwise `effective_since = max(down_since, suppressed_until or down_since)`;
  `now - effective_since >= offline_minutes` → conditional `UPDATE … SET state='persisted',
  persisted_at=now, outage_id=make_outage_id(ci_id, down_since), rule_json=… WHERE
  monitor_id=? AND state='open'`; `rowcount == 1` → **once** `ctx.emit(TOPIC_OUTAGE_PERSISTED,
  {...})` (local). Without `ci_id` on the monitor (also not via the host monitor): no event, log
  once per monitor.
- Payload: `outage_id, ci_id, kind, name (monitor name), host_ci_id (ci_id of the host monitor,
  empty for host monitors), monitor_id, since, persisted_at, duration_s, rule {offline_minutes,
  severity, auto_resolve}, error (latest_error)`.
- Recovery event see section 4; `duration_s = recovered_at - down_since`.

## 6. State on the dashboard

- `aggregate_state` (`app/ui/styles.py:78-90`) and React equivalent: `DOWN > UNKNOWN >
  MAINTENANCE > UP > PAUSED`.
- `status_counts`/`stats` (1232-1284): count `maintenance`; `health()` unchanged.
- `_serialize_monitor` (`models.py:162`), `list_monitors` (1286), `helpers.py:147,271-307`
  (whitelist dicts, host/service views 180-200): fields `ci_id`, `in_maintenance`,
  `maintenance_until`, `maintenance_source`, `outage_since` (for an open/persisted row).
  Overview cache (TTL 10 s) is cleared on window change (`_view_cache.clear()`).
- `_sync_job_on_loop` (1550): `MAINTENANCE` keeps the job (probing continues, recovery
  is detected). `_load_probe_candidates` (894-903): treat `MAINTENANCE` like `DOWN`.

## 7. API (`app/controller/api.py`, R/W as before)

| Method | Path | Perm | Content |
|---|---|---|---|
| GET | `/windows?ci_id=&monitor_id=&source=` | R | windows incl. `effective_state` per row |
| POST | `/windows` | W | manual window `{ci_id \| monitor_id, windows: [input or normal form], note}`; validation via `parse_windows` (400 with messages) |
| DELETE | `/windows/{id}` | W | only `source == "monitoring"` |
| GET | `/overrides?active=true` | R | overrides with `active`, `until_effective` |
| POST | `/overrides` | W | `{ci_ids \| monitor_ids, minutes (<= override_max_minutes), reason}` → `override_id = "mon-<uuid>"` |
| DELETE | `/overrides/{id}` | W | ends (`ended_at=now`, linger) — only own (`source == "monitoring"`) |
| GET | `/outages` | R | open/persisted outages with rule and effective duration |

Paths `/maintenance/*` remain for plugin maintenance; `/settings` is reserved.

## 8. UI (`src/ui/PluginApp.tsx`, `src/ui/lib/api.ts`, `locales/monitoring.{de,en}.json`)

- `MonitorState` (12) + `MAINTENANCE`; `STATE_COLOR` (140-149)
  `var(--lx-state-maintenance, var(--lx-state-paused))`; `STATE_LABEL`/`StateBadge` (320-353)
  with addition "until hh:mm" from `maintenance_until`; `HeaderCard` counter (1053-1059) "Maintenance".
- Types `ServiceView`, `Host`, `Row` (14-66): `ci_id?`, `in_maintenance?`, `maintenance_until?`,
  `outage_since?`.
- New components (own files under `src/ui/`): `MaintenanceDialog` (create override:
  duration preset 30/60/120/240 min, reason; optionally recurring window weekly/once) and
  `OverridesPanel` (active overrides with "End"). Hook-up: button in `HostCardDetail`
  (519), `SplitView` (771), toolbar (873) for multi-select, panel next to `HeaderCard`.
- NiceGUI (`app/ui/styles.py:8-27`, `widget.py:25`, `_components/host_card.py`): color and
  label for `MAINTENANCE`; no dialog (React is the target).

## 9. Cluster

- Prober lease unchanged; the outage job hangs on `start()`/`stop()` like the other
  housekeeping jobs. Standby instances keep the cache current for API responses (reload via
  `monitors_changed reason=maintenance`), but do not evaluate.
- DB writes of the handlers with `_rc_session` + `_retrying`; `replace_windows` in a short
  transaction; IntegrityError = "peer was faster".
- `_FakeCtx.emit` in the tests gets `cluster=False`, so that the `cluster=True` path is tested
  (today hidden behind the `TypeError` branch 485-488).

## 10. Tests (`tests/`, recipe in `analysis-monitoring.md` §7)

- `test_maintenance.py` (existing file covers plugin maintenance → new file
  `test_maintenance_windows.py`): windows replace per `source`; override start/end/linger/expiry;
  cache reload on `monitors_changed reason=maintenance`; manual windows emit with
  `source=monitoring` and are ignored by own handler; validation errors 400.
- `test_store_heartbeat_maintenance.py`: DOWN in window → `MAINTENANCE` stored, aggregate
  `maintenance_samples`, uptime denominator; UP in window stays UP; outage row is created,
  `suppressed_until` advanced, deleted on UP; passive results handled the same;
  `PassiveResult` with `MAINTENANCE` → 400.
- `test_outage_rule.py` with fake clock (`monkeypatch` on `models._utc_now` and the
  maintenance clock): persisted after `offline_minutes` outside window; not inside; clock
  restarts after window end; rule cascade service CI → host CI → settings; emergency switch
  per kind; without `ci_id` no event; double evaluation yields exactly one emit (conditional
  UPDATE); recovery emits after commit with `duration_s`; payload fields complete.
- `test_inventory_ci_id.py`: ingest takes over `ci_id`/`host_ci_id`; change of `ci_id`
  changes `config_hash`.

## 11. Docs and acceptance

- `documentation.md` (DE): rewrite section "Events" (from 185) (current state plus new
  topics), new section "Maintenance windows, overrides and outage rule", settings table;
  correct `docs/index.md:8`; correct the React route in `.claude/skills/run-state-monitoring/SKILL.md`; CHANGELOG 1.7.0.
- Acceptance: `00-ergebnis-p0.md` section 7, steps 3–6. `ruff`, `black --check`, `mypy`;
  bundle rebuilt; commit on `feature/maintenance-windows`; tags `v1.7.0`/`1.7.0` after
  approval.
