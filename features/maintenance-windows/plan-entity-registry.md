# Sub-plan P5a: Entity Registry 0.3.0 — read model `maintenance`, scheme `iac.service`

Execution: Fable subagent. Basis: `analysis-entity-registry.md`, `00-ergebnis-p0.md`
(sections 1–4). Repo `plugins/lyndrix-plugin-entity-registry` (canonical layout).
**Roll out before the orchestrator** (scheme `iac.service`). Core library optional:
`try: from core.api import maintenance except ImportError: maintenance = None` → without
the library only raw data is delivered (`active: null`).

## 1. Manifest and catalog

- `entrypoint.py:75` `version="0.3.0"`; `min_core_version` stays `1.4.0`.
- `permissions.subscribe` (94-103) + `maintenance:windows_published`,
  `maintenance:override_started`, `maintenance:override_ended`.
- `catalog/entity_types.yml` `identifier_schemes`: new
  `{id: iac.service, label: IaC service instance, description: "<host>/<service-name> — a
  service.yml deployed on one host", priority: 20}`. Kinds unchanged (`service` exists).
- CHANGELOG; `documentation.md` (DE): table overview (31), bus table (110ff), new route,
  section "Maintenance windows on the CI".

## 2. Data model (`app/model/models.py`, DDL via `create_all` under advisory lock)

```
registry_maintenance  id PK, entity_uuid String(36) FK→registry_entities.uuid idx,
                      kind String(16)  -- window | override
                      source String(64), override_id String(64) NULL,
                      windows JSON NULL, inherited_from String(32) NULL, deploy_policy String(16) NULL,
                      incidents JSON NULL,
                      until DateTime NULL, started_at DateTime NULL, ended_at DateTime NULL,
                      linger_minutes Integer NULL, reason Text NULL, updated_at
                      UNIQUE (entity_uuid, source, kind, override_id)
```

No use of `Entity.attributes` (reasoning analysis §9.1: no namespace, RMW race,
event noise).

## 3. Logic (`app/logic/service.py`, new methods; all via `self._write` with retry)

- `apply_windows(source, items, replaces: bool)`: transaction: `replaces` → `DELETE … WHERE
  source=? AND kind='window'`; per item `_resolve_live(ci_id)` (tombstones follow, unknown → 
  silently skip, counter into the log) → INSERT (`windows`, `inherited_from`, `deploy_policy`,
  `incidents`). The kind field of the item is ignored (type is set on the entity). **No**
  `entity_updated` per entity; one collective event `entity_registry:maintenance_changed
  {source, count}` (new in `permissions.emit`) for UIs.
- `start_override(payload)`: upsert per `(entity_uuid, source, 'override', override_id)` for every
  `ci_id` in `ci_ids`; `end_override(payload)`: set `ended_at`, `linger_minutes` (all
  rows of the `override_id`).
- `maintenance_for(s, uuids: list[str], now) -> dict[uuid, dict]`: one `IN` query; per entity
  `windows` (all sources), overrides (only active ones after `override_active`), result
  `{"active": bool|None, "until": iso|None, "source": "window"|"override"|None, "next_start":
  iso|None, "windows": [{source, windows, inherited_from}], "overrides": [{id, until, reason,
  source}], "incidents": {...}|None, "deploy_policy": str|None}` via `core.api.maintenance.effective_state`; without library `active=None` and raw data.
- `purge_expired(now)`: delete overrides with `until + linger < now − 1 day`; called occasionally from
  `end_override`/`start_override` (no timer in the plugin).
- `_entity_detail` (156-190): field `maintenance`; `_summaries`/`list_entities` (233-236,
  314-320): field `maintenance` compact (`active`, `until`, `source`) via batch query.

## 4. Bus handler (`entrypoint.py`, pattern `_on_relate` 275-293)

Three `@ctx.subscribe` handlers: payload check (`isinstance dict`, `items`/`override_id`),
`svc.is_ready`, `asyncio.to_thread`, errors via `_fail`. Idempotent (unique key, delete+insert
per `source`). Local delivery suffices (state in the DB, plugin on every instance).

## 5. API (`app/api/__init__.py`)

| Method | Path | Perm | Content |
|---|---|---|---|
| GET | `/entities/{uuid}` | read | contains `maintenance` (section 3) |
| GET | `/entities?in_maintenance=true` | read | filter (after batch computation, page limited) |
| GET | `/maintenance?ci_ids=a,b,c` (max 200) | read | batch read model for consumers/UIs |
| GET | `/entities/{uuid}/host` | read | host entity via outgoing `runs_on` relation, 404 if none |

## 6. UI (`app/ui/react/PluginApp.tsx`, `lib/api.ts`, `locales/entity_registry.{de,en}.json`)

- `MaintenanceBadge` (class `lx-badge--maintenance`, fallback `lx-badge--paused`, icon
  `engineering`, tooltip source/end) in the type cell of the list (207) and in the detail header
  (285).
- Detail: new card "Maintenance" before the ITSM card (367): current window (until), next
  window (from), overrides (reason, end, source), sources of the windows with `inherited_from`,
  incident rule.
- Type `Entity` (`lib/api.ts:27ff`) + `maintenance?`. Rebuild and commit bundle `app/ui/static/ui_bundle.js`.

## 7. Tests (`tests/test_registry.py` fixture `svc`, new file `tests/test_maintenance.py`)

- `apply_windows` replaces per `source`, leaves foreign sources in place; unknown `ci_id`
  skipped; tombstone UUID lands on the target; duplicate delivery idempotent.
- Override start/end/linger/expiry; `maintenance_for` with and without library (monkeypatch
  `maintenance = None`); `effective_state` with simultaneous window and override.
- Detail and list contain `maintenance`; filter `in_maintenance`; batch route; `/host` via
  `runs_on`; catalog loads `iac.service`; `identify` with `iac.service` works.
- Bus handler with `_FakeCtx` (previously untested): invalid payload → `ingest_failed`.

Run in the dev core (`docker cp` + `PYTHONPATH=/app`), `ruff`, `black --check`.

## 8. Acceptance

Dev bench: orchestrator 1.6.0 publishes → `GET /entities/<host>` shows the window; an override from
monitoring appears and disappears after linger. Tags `v0.3.0`/`0.3.0` after approval.
