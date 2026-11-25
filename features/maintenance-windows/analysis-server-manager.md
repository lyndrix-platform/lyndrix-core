# Analysis: Server Manager (sweep for plan section 7.4 / 10)

Repo: `plugins/lyndrix-plugin-server-manager` (paths below are relative to it). Read-only access, state as of 2026-10-17.

## 1. Overview

- Manifest ID `lyndrix.plugin.server_manager`, version `1.3.0`, `min_core_version="0.0.6"`, `auto_enable_on_install=False` (`entrypoint.py:78-89`); `react_ui=True`, `i18n_namespace="server_manager"`, routes `/server-manager` (sidebar) and `/server-manager/settings` (`entrypoint.py:91-111`).
- `permissions.subscribe`: `db:connected`, `server_manager:register_server`, `:deregister_server`, `:writeback_failed`, `:catalog_changed`, `plugin:settings_changed` (`entrypoint.py:113-122`).
- `permissions.emit`: `server_created`, `server_updated`, `server_deleted`, `hardware_changed`, `status_changed`, `ingest_failed`, `writeback_requested`, `catalog_changed` (`entrypoint.py:123-132`).
- Roles `admin`/`operator` (api:read+write), `viewer` (api:read), each as `plugin:lyndrix.plugin.server_manager:api:*` (`entrypoint.py:137-152`).
- **No `settings_schema`, no `placement`, no `panels`, no `ai_instructions`** in the manifest. Settings = NiceGUI `render_settings_ui` (`entrypoint.py:157`, `app/ui/settings.py:15`) and React `SettingsView` (`src/ui/PluginApp.tsx:380`); the only setting is the catalog path, persisted via `ctx.set_secret("catalog_dir")` (`app/controller/service.py:74,153`).
- The plugin **does not subscribe to `iac:inventory_updated`** (grep finds nothing in the repo). Hosts arrive exclusively via `server_manager:register_server`/REST (section 3).
- The layout deviates from the canon: `app/controller/` instead of `app/logic/`, `src/ui/` instead of `app/ui/react/` (directory listing; `CLAUDE.md` mentions the controller→logic rename, not yet done here). The bundle lives in `ui_static/ui_bundle.js` (legacy path).

## 2. Data model

One table `server_manager_servers` (`app/model/models.py:12-13`, own `_Base`, `models.py:8`):

| Column | Type | Evidence |
|---|---|---|
| `id` | Integer PK | `models.py:21` |
| `name` (NN), `hostname` (idx) | String(255) | `models.py:22-23` |
| `environment_id` (NN) | String(64), format `site:stage` | `models.py:24`; orchestrator value `engine.py:1262` |
| `server_type` (NN) | `physical|vm|container` | `models.py:25` |
| `product_id`, `service_class_id`, `os_family_id`, `os_version_id`, `os_type` | String | `models.py:26-30` |
| `status` | String(64), default `active` | `models.py:32` |
| `hardware_profile` (JSON), `tags` (JSON), `notes` (Text) | | `models.py:33-35` |
| `source`, `external_id` | String(64)/(255), NULL = manual | `models.py:39-40` |
| **`ci_id`** | String(36), index `ix_server_manager_servers_ci_id` | `models.py:43`, `database.py:51` |
| `writeback` (Bool), `sync_state` (`pending|failed|NULL`), `sync_error` | | `models.py:47,51-52` |
| `ordered_by`, `ordered_at`, `created_at`, `updated_at` | | `models.py:57-60` |

- Unique `uq_server_manager_source_ext (source, external_id)` (`models.py:17-19`); NULLs do not collide (manual records).
- `read_only` = `source is not None and not writeback` (`models.py:54-56`); `to_dict()` returns all fields incl. `ci_id`, `read_only` (`models.py:62-89`).
- **`ci_id` is not resolved by the plugin.** No `entity_registry:identify`/`lookup` in the code (grep empty). It comes from the pushing source (the orchestrator sets `"ci_id": host.get("ci_id")`, `plugins/lyndrix-plugin-iac-orchestrator/app/controller/engine.py:1264`; the orchestrator fetches it beforehand via `_attach_ci_ids`, `engine.py:1285`) or by hand (`ServerCreatePayload.ci_id`, `app/api.py:54`; length exactly 36, `api.py:54,74`). `ci_id` is in `_UPDATABLE_FIELDS` (`service.py:60-66`), so every push overwrites it if it is sent along and differs.
- Migration: additive `ALTER TABLE ... ADD COLUMN` from `_NEW_COLUMNS` (`database.py:34-46`), indexes `_INDEXES` (`database.py:49-52`), under `db_instance.advisory_lock("lyndrix_bootstrap")` (`database.py:71-96`), race codes 1050/1060/1061 tolerated (`database.py:22-23`).
- **Relations: none.** Neither services/containers per host nor parent/child (hypervisor→VM). `tags` are only Ansible group names (the orchestrator filters `site_`/`stage_`, max. 25, `engine.py:1266-1268`). `hardware_profile` is free-form JSON per `server_type`. There is no column for VM ID or hypervisor (the orchestrator sends `terraform.vm_id` only to derive `server_type`, `engine.py:1247-1253`, and does not pass it on).

## 3. Ingest paths and events

**Ingest (all end in `ServerManagerService.upsert_external_server`, `service.py:382-418`):**

| Path | Location | Behaviour |
|---|---|---|
| Bus `server_manager:register_server` | `entrypoint.py:281-296` | Payload not a dict → `ingest_failed`; `wait_ready()` (max 30 s, `service.py:122-128`); validation via `ExternalServerPayload` (`api.py:88-97`), `asyncio.to_thread(upsert)` |
| Bus `server_manager:deregister_server` | `entrypoint.py:298-317` | `{source, external_id}` → `remove_external_server` (`service.py:420-424`) |
| Bus `server_manager:writeback_failed` | `entrypoint.py:319-331` | `mark_writeback_failed` (`service.py:426-451`) sets `sync_state="failed"` |
| REST `PUT /servers/external/{source}/{id}` | `api.py:371-380` | `ExternalServerBody` (`api.py:77-85`), 400 on ValueError, 409 on LookupError |
| REST `POST /servers/import` (batch, max 1000) | `api.py:395-417` | own result per item |

- Upsert: key `(source, external_id)` via `_find_external_id` (`service.py:373-380`); new → required fields `name, environment_id, server_type` (`service.py:69,401-403`), `IntegrityError` on parallel insert → treated as update (`service.py:404-411`); otherwise PATCH semantics only on `_UPDATABLE_FIELDS` (`service.py:397,415`). A push resets `sync_state/sync_error` and adopts `writeback` (`service.py:291-297`). Manual records (source NULL) are never hit (test `tests/test_ingest.py:79`).
- Orchestrator payload (the only known producer): `source="iac-orchestrator"`, `external_id=host["name"]`, `name`, `hostname`, `environment_id`, `server_type`, `status="active"`, `writeback=False`, `ci_id`, `tags`, `notes` (`engine.py:1255-1269`). Triggered in `emit_iac_inventory_updated` (`engine.py:1274-1294`), in parallel to `iac:inventory_updated {hosts}` (`engine.py:1286`), which the Server Manager ignores.
- There is **no deletion of orphaned hosts** by full sync; `deregister_server` must be sent explicitly (no `replaces` semantics in the plugin).

**Emitted events (`ServerManagerService._emit`, `service.py:518-534`, thread-safe via `call_soon_threadsafe`):**

| Topic | Payload | Location |
|---|---|---|
| `server_manager:server_created` | `to_dict()` | `service.py:247` |
| `server_manager:server_updated` | `to_dict() + changes{field:{old,new}}` (only `_UPDATABLE_FIELDS`, hence incl. `ci_id`) | `service.py:307-313` |
| `server_manager:server_deleted` | last `to_dict()` | `service.py:364` |
| `server_manager:hardware_changed` | `{server_id, server_name, action, old_profile, new_profile, environment_id, server_type}` | `service.py:250,316` |
| `server_manager:status_changed` | `{server_id, server_name, old_status, new_status}` | `service.py:327` |
| `server_manager:ingest_failed` | `{topic, source, external_id, error}` | `entrypoint.py:271-279` |
| `server_manager:writeback_requested` | `{action, source, external_id, server, changes}`; **local bus only** (no `cluster=True`) | `service.py:507-516` |
| `server_manager:catalog_changed` | `{catalog_dir, action}`, `cluster=True` | `service.py:181-186` |

Additionally consumed: `plugin:settings_changed` (reloads the catalog path, `entrypoint.py:263-266`), `db:connected` (bootstrap, `entrypoint.py:251-254`).

## 4. API routes

Mount `/api/plugins/lyndrix.plugin.server_manager/` (`entrypoint.py:237`); permissions `plugin:<id>:api:read|write` (`api.py:34-36`). All handlers use `asyncio.to_thread`; DB routes return 503 until `is_ready` (`api.py:290-296`).

| Method | Path | Perm | Location |
|---|---|---|---|
| GET | `/servers` | read | `api.py:299` |
| GET | `/servers/{server_id}` | read | `api.py:304` |
| POST | `/servers` | write | `api.py:315` |
| PATCH | `/servers/{server_id}` (409 if read-only) | write | `api.py:329` |
| DELETE | `/servers/{server_id}` | write | `api.py:345` |
| PUT | `/servers/external/{source}/{external_id}` | write | `api.py:371` |
| DELETE | `/servers/external/{source}/{external_id}` | write | `api.py:382` |
| POST | `/servers/import` | write | `api.py:395` |
| GET | `/statuses` | read | `api.py:420` |
| GET | `/stats` | read | `api.py:424` |
| POST | `/validate` | read | `api.py:430` |
| POST | `/catalog/evaluate-combo` | read | `api.py:447` |
| GET | `/catalog` | read | `api.py:469` |
| POST | `/catalog/reload` | write | `api.py:473` |
| POST | `/catalog/path` | write | `api.py:481` |

There is no `/health`, `/resolve` or `/ai/instructions` route; `/settings` is (correctly) not used. There is no `?ci_id=` filter on `/servers`.

## 5. UI components

React (`src/ui/`): `index.tsx` (10 lines), `PluginApp.tsx` (533 lines), `ServerWizard.tsx` (1084 lines), `lib/api.ts` (340 lines).

- `PluginApp` (`PluginApp.tsx:529`): router only via `pathname.endsWith('/settings')`; no `panel` prop, no drawer/detail view.
- `ServerManagerView` (`:504`): state `list | form`; clicking a row opens **the wizard** (`ServerWizard`, `:158`), only for non-read-only records (`:297-299`). Read-only (IaC) hosts have **no detail view**.
- `ServerListView` (`:162`): loads `serverManagerApi.listServers()` (`:183`), filters environment/status/type (`:249-262`, `useMemo` `:196`), `lx-table` with columns Name / Connection / Status / Actions (`:284-293`).
- Row cells: the connection cell (`:303-337`) contains `hostname · environment · type` plus the ci_id icons "Open registry" (`:306-322`) and "Report incident" (`:323-336`); the status cell (`:338-342`) = `<StatusBadge>` (`:90`) + `<SyncIndicator>` (`:106`); the action cell (`:344+`, lock icon for read-only).
- Type `ServerRecord` in `src/ui/lib/api.ts:6-35` (incl. `ci_id`); API wrapper `serverManagerApi` (`api.ts:~300-316`).
- Styles use `lx-badge`, `lx-dot`, `--lx-state-up/down/paused/unknown` (`PluginApp.tsx:81-103`).
- i18n: `locales/server_manager.{de,en}.json`, hook `useTranslation('server_manager')` (`PluginApp.tsx:107`).

NiceGUI (still present and active): `@ui.page("/server-manager")` (`entrypoint.py:333-336`), `app/ui/page.py` (header, stats, list), `app/ui/overview.py` (list, status colours `:192`, environment badge `:220`), `app/ui/widget.py` (dashboard widget), `app/ui/settings.py`, 3-step configurator `app/controller/configurator/step{1,2,3}.py`. The NiceGUI page imports `ui.layout.main_layout` from core internals with a fallback (`entrypoint.py:57-67`).

## 6. Test status (reference recipe)

- 3 files, 36 tests: `tests/test_smoke.py` (catalog parser, rules; 8 tests), `tests/test_ingest.py` (upsert, idempotency, writeback, thread emit; 14), `tests/test_cluster.py` (DDL under advisory lock, race tolerance, 1020 retry, catalog path; 12).
- `tests/conftest.py:9` imports `core.services` before everything else (circular import `core.api` ↔ `ui.layout`, TODO in the file). Consequence: tests need the core on `PYTHONPATH`; `app.*` is imported from the repo root (not via `entrypoint`).
- Fixture pattern (`tests/test_ingest.py:19-42`): `_FakeCtx` with an `events` list and `emit(topic, payload)` (in `test_cluster.py:21` with a `cluster=False` parameter, plus `set_secret/get_secret`), in-memory SQLite `create_engine("sqlite://", poolclass=StaticPool, check_same_thread=False)`, `fake_db = SimpleNamespace(engine=, SessionLocal=sessionmaker(...), is_connected=True)` via `monkeypatch.setattr(database, "db_instance", fake_db)`; then `ServerManagerService()`, `set_context(_FakeCtx())`, `on_db_connected()`. `test_cluster.py:37` adds `advisory_lock=lock` to the fake.
- Invocation: `pip install -r requirements-dev.txt` (pytest, pytest-asyncio, pytest-cov, mypy/ruff/black; `requirements-dev.txt:1-3`) and `pytest` in the repo root (`documentation.md:148-167`). According to the workspace rule (CLAUDE.md "DB gotchas") inside the container `lyndrix-core-dev` via `docker cp` + `PYTHONPATH=/app`, and on the cluster bench additionally `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local`. Not executed (read-only sweep).
- Not tested: entrypoint/`setup(ctx)` handlers, API router (no `TestClient`), React UI. New handlers (section 8) should be built as pure service methods so that the existing fixture suffices.
- Dev run: `.claude/skills/run-server-manager/SKILL.md` (the plugin is mounted into `docker-compose.dev.yml` at `/app/plugins/server_manager`).

## 7. Cluster specifics

- **No leases, no `placement`, no `ctx.on_lease`, no `ctx.my_shard`, no `ctx.set_status`** (grep empty). All instances process all bus events; one-off work is not coordinated.
- Bus-driven writes are nevertheless safe because they are idempotent: upsert by unique key with an IntegrityError fallback (`service.py:404-411`), `retry_write` on 1020/1205/1213 (`database.py:149-163`), READ COMMITTED session (`database.py:166-189`), DDL under the `lyndrix_bootstrap` lock.
- Important for section 8: `register_server` is the **local bus** (orchestrator `ctx.emit` without `cluster=True`, `engine.py:1255`); it lands only on the instance where the orchestrator sync runs (`pinned:runner`). The DB is shared, so all instances see the result. A `maintenance:*` event, by contrast, would have to be read from the DB by the peers (UI/API run on every instance); in-process state is not enough.
- `server_manager:writeback_requested` is deliberately local only; TODO for a leased re-emitter (`service.py:18`).
- `catalog_changed` is the only `cluster=True` emit (`service.py:185`); received only with `payload["_origin"]` (`entrypoint.py:259`).
- `health()` checks DB, tables, `COUNT(*)`, catalog (`entrypoint.py:167-216`), reports standby correctly (no lease concept).

## 8. Attachment points for plan 7.4

**Starting point:** per host the Server Manager only knows `ci_id` (nullable) and `(source, external_id)`. Hosts from the orchestrator already carry `ci_id` (`engine.py:1264`), manual ones only if the operator sets it.

### 8.1 Read model (new table recommended)
- **New table** `server_manager_maintenance` (model in `app/model/models.py`, same `_Base`). Proposal: `ci_id` (String(36), idx), `kind` (`window|override`), `ref` (`override_id` or window index), `source` (String(64)), `schedule` (JSON, schedule dicts from plan §5), `inherited_from` (String(36) NULL), `until` (DateTime NULL, override only), `reason` (Text), `updated_at`. Unique `(kind, ci_id, ref)` or, for windows, `(source, ci_id)` with a JSON list.
  Rationale against a JSON column on `server_manager_servers`: events arrive per `ci_id`, also for CIs that have no server record (yet) (kind `service`, order `windows_published` before `register_server`); `replaces: true` per `source` (plan §6) can be implemented cleanly as `DELETE WHERE source=? AND kind='window'` + insert in one transaction. A JSON column could only be written on a `ci_id` match and, although a `register_server` push does not overwrite it (PATCH only on `_UPDATABLE_FIELDS`, `service.py:397`), it would never be back-filled when a ci_id is assigned later.
- `app/model/database.py`: the new table is created automatically via `create_all` (`database.py:89`) and falls under the advisory lock; no `_NEW_COLUMNS` entry needed. Risk: the migration logic only checks `_TABLE` (`database.py:120,136`); `create_all` suffices for the new table.
- `app/controller/service.py`: new methods `apply_windows_published(payload)`, `apply_override_started(payload)`, `apply_override_ended(payload)` (blocking, `retry_write`, short READ COMMITTED transactions, chunks because of `max_statement_time`), plus the read method `maintenance_for(ci_ids) -> dict[ci_id, {windows, active_override, effective_until}]`. Compute the effective window/`active_until`/`next_start` from the planned `core.api.maintenance` (API 1.13, plan §5) at read time; **do not** store it in the DB, because it depends on `now`. Fallback on an older core: `getattr` guard on the import, otherwise show only the raw data.
- `entrypoint.py`: three `@ctx.subscribe(...)` handlers analogous to `_on_register_server` (`entrypoint.py:281-296`): `isinstance(payload, dict)` check, `await svc.wait_ready()`, `asyncio.to_thread(...)`, only log errors (the topics do not belong in `ingest_failed`, whose payload has the `source/external_id` shape; possibly a separate log). Ignore unknown fields (plan §6).
- `entrypoint.py` manifest: extend `permissions.subscribe` with `maintenance:windows_published`, `maintenance:override_started`, `maintenance:override_ended` (`entrypoint.py:113-122`). Bump the version to the next minor (1.4.0), do not raise `min_core_version` as long as the code stays `getattr`-guarded.
- **Cluster risk:** The emitter must send the maintenance events with `cluster=True`, otherwise they reach only one instance; since the state lives in the shared DB, double processing (every instance writes the same row) and lost events (instance not reached) are the two cases. Double: can be solved idempotently (delete+insert per `source`, upsert by override ID). Lost: acceptable, because the orchestrator re-sends `replaces: true` on the next sync; overrides have `until` and expire anyway. Peer events carry `payload["_origin"]` (pattern `entrypoint.py:259`) – do not filter here, otherwise events from the own instance get lost; write idempotently instead.
- Cleanup: ignore expired overrides (`until < now`) on read and delete them occasionally on write; no lease needed.

### 8.2 Display on the host
- `app/api.py`: variant A (minimal, backward compatible): enrich the `to_dict()` result in `list_servers`/`get_server` (`api.py:299-313`) with a field `maintenance: {windows[], active_override, in_maintenance, next_start, active_until}`, using **one** batch query over all `ci_id` (no N+1; tables are small). Variant B: separate routes `GET /servers/{id}/maintenance` and `GET /maintenance?ci_id=` (read perm). Recommendation: A plus B for the detail view; `ServerRecord.to_dict()` (`models.py:62`) stays untouched, since the enrichment happens in the service.
- `src/ui/lib/api.ts:6-35`: extend `ServerRecord` with an optional `maintenance?: ...`.
- `src/ui/PluginApp.tsx`: new component `MaintenanceBadge` next to `SyncIndicator` (`:106`) and integration in the status cell (`:338-342`); tooltip with source, `active_until`/`next_start`. No change needed in the connection cell (`:303-337`). An "in maintenance" filter can be added to the selects (`:249-262`) (logic in the `useMemo` `:196`).
- **Detail view is missing:** There is no drawer; for read-only hosts the row does not react to clicks (`:297-299`). Showing "effective window + running overrides" needs a new detail/expand view (new component `ServerMaintenancePanel`, in `ServerManagerView` `:504` as a third `view.kind`, or an expandable row). This is the largest UI effort; the wizard (`ServerWizard.tsx`) is unsuitable for it (edit form, not reachable for read-only hosts).
- i18n: new keys in `locales/server_manager.de.json` and `.en.json` (`i18next` dialect `{{name}}`).
- NiceGUI page (`app/ui/overview.py:192-220`): stays without display for now (React is the migration target); when weighing options, possibly add a badge later.

### 8.3 Services per host
**Not possible without an extension.** Neither a relation nor a list exists in the plugin (section 2). Options: (a) the Server Manager asks the Entity Registry on demand (`entity_registry:lookup`, CLAUDE.md "Configuration-item ids") or fetches services via Docker Manager/orchestrator – this would be a new optional `ctx.request` call (reply `None` → degrade); (b) the affected services are supplied by the emitter in the `windows_published` event (`inherited_from`, plan §6, covers host→service inheritance but not the opposite direction); (c) the UI only links to Registry/Monitoring. Recommendation: (c) in the first iteration, "affected services" via a registry deep link (pattern `PluginApp.tsx:306-322`).

### 8.4 Tests
New file `tests/test_maintenance.py` with the fixture from `tests/test_ingest.py:19-42` (SQLite, `_FakeCtx`); cases: `replaces` per source, idempotency, override end, expiry, event for a CI without a server record, SQLite with `PRAGMA foreign_keys=ON`.

## 9. Deviations from the plan / open questions

1. Plan §6 names the Server Manager as a consumer of `maintenance:windows_published`, and plan §4.2/§7.4 expects `ci_id` per host. In reality: `ci_id` is nullable and only set if the source sends it; manual hosts usually have none. Events without a matching host record must either be stored (recommended, separate table) or discarded – a decision is needed.
2. The plan §10 briefing expects "`iac:inventory_updated` usage" in the Server Manager. In reality: none; the orchestrator pushes `server_manager:register_server` instead (`engine.py:1255`). This is uncritical for maintenance windows, since §6 uses its own events.
3. Plan §10 expects detail/drawer display locations. In reality: only the table row; no detail view exists (section 5). Should one be newly created or is the badge enough?
4. No way to list "affected services" (section 8.3).
5. `register_server`/`writeback_requested` are local-bus; for `maintenance:*` the emitter must set `cluster=True` – not specified in plan §6, please add.
6. Directory structure is not canonical (`controller/`, `src/ui/`); the new build should keep the existing structure (rename separately, not in this feature).
7. No core time-zone/cron helper exists until `core.api.maintenance` (API 1.13) exists; `min_core_version` stays 0.0.6 only if the display degrades without the library.
8. Open: should the Server Manager be able to trigger manual overrides (plan §7.4 says "later" for Docker Manager/External Services)? Currently display only; no emit right needed (`permissions.emit` stays unchanged).
