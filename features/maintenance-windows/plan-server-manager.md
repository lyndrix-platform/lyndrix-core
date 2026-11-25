# Sub-plan P5b: Server Manager 1.4.0 — maintenance badge on the host

Execution: Fable subagent (smallest sub-plan). Basis: `analysis-server-manager.md`,
`00-ergebnis-p0.md`. Repo `plugins/lyndrix-plugin-server-manager`, layout stays
`app/controller/` + `src/ui/`. Core library optional (`try/except ImportError`),
`min_core_version` stays `0.0.6`. Decision E: only badge, tooltip and filter; no
detail view in this release.

## 1. Manifest

`entrypoint.py:78-89` `version="1.4.0"`; `permissions.subscribe` (113-122) +
`maintenance:windows_published`, `maintenance:override_started`, `maintenance:override_ended`.
`permissions.emit` unchanged. CHANGELOG, `documentation.md` (DE), locales de/en.

## 2. Data model (`app/model/models.py`, same `_Base`; `create_all` in `database.py:89`)

```
server_manager_maintenance  id PK, ci_id String(36) idx, kind String(16)  -- window | override
                            source String(64), override_id String(64) NULL,
                            windows JSON NULL, inherited_from String(32) NULL,
                            until DateTime NULL, started_at DateTime NULL, ended_at DateTime NULL,
                            linger_minutes Integer NULL, reason Text NULL, updated_at
                            UNIQUE (ci_id, source, kind, override_id)
```

Events for `ci_id`s without a server record are **stored** (order `windows_published`
before `register_server` is normal); the display joins on read via `servers.ci_id`.

## 3. Logic (`app/controller/service.py`)

- `apply_windows_published(payload)`, `apply_override_started(payload)`,
  `apply_override_ended(payload)`: blocking, `retry_write`, READ COMMITTED, `replaces` →
  `DELETE WHERE source=? AND kind='window'` + INSERT in one transaction; upsert per
  `override_id`; occasionally `purge_expired`.
- `maintenance_for(ci_ids, now) -> dict[ci_id, {active, until, source, next_start, overrides:
  [...]}]` via `core.api.maintenance.effective_state` (without library: `active=None`).
- `list_servers`/`get_server` (service page, not `to_dict`): field `maintenance` per record with
  a set `ci_id`, via **one** batch query.

## 4. Bus handler (`entrypoint.py`, pattern `_on_register_server` 281-296)

Three handlers: `isinstance(payload, dict)`, `await svc.wait_ready()`, `asyncio.to_thread(...)`,
log errors (not `ingest_failed`, whose payload has `source/external_id` shape).
Do not filter `payload["_origin"]` (idempotency instead of filtering).

## 5. API (`app/api.py`)

- `GET /servers`, `GET /servers/{id}`: field `maintenance` (additive).
- `GET /servers?in_maintenance=true`: filter.
- `GET /maintenance/{ci_id}`: full read model (windows per source, overrides) for the
  tooltip/later detail.

## 6. UI (`src/ui/PluginApp.tsx`, `src/ui/lib/api.ts`, `locales/server_manager.{de,en}.json`)

- `ServerRecord` (`api.ts:6-35`) + `maintenance?`.
- `MaintenanceBadge` next to `SyncIndicator` (106) in the status cell (338-342): label
  "Maintenance until hh:mm" or "Maintenance from …" (next window within 24 h), tooltip with source,
  reason, end. Class `lx-badge--maintenance` with fallback `lx-badge--paused`.
- Filter "In maintenance" with the selects (249-262), logic in the `useMemo` (196).
- In the connection cell (303-337) an icon "Open registry" already exists;
  "affected services" only as a deep link into the registry (`/apps/lyndrix-plugin-entity_registry/registry?uuid=<id>`).
- NiceGUI (`app/ui/overview.py:192-220`): no change.
- Rebuild and commit bundle `ui_static/ui_bundle.js`.

## 7. Tests (`tests/test_maintenance.py`, fixture from `tests/test_ingest.py:19-42`)

`replaces` per source; idempotency on duplicate delivery; override start/end/linger/expiry;
event for `ci_id` without record is stored and visible after `register_server`;
`list_servers` returns `maintenance` only for records with `ci_id`; filter `in_maintenance`;
behavior without core library. SQLite with `PRAGMA foreign_keys=ON`.

## 8. Acceptance

Dev bench: host with window from the orchestrator shows the badge; manual override from
monitoring appears. `ruff`, `black --check`, `mypy`. Tags `v1.4.0`/`1.4.0` after approval.
