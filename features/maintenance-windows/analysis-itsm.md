# Analysis of the ITSM plugin for maintenance windows (plan section 7.3)

Read-only sweep, state `lyndrix-plugin-itsm` 0.2.0. Paths relative to
`/home/marvin/gitlab/lyndrix-dev/plugins/lyndrix-plugin-itsm`. Line references `file:line`.

## 1. Overview

- Manifest ID `lyndrix.plugin.itsm` (`entrypoint.py:69`), version `0.2.0` (`entrypoint.py:76`),
  `min_core_version="1.4.0"` (`entrypoint.py:85`), `auto_enable_on_install=True` (`:86`).
  No `placement` set (default `any`), no lease usage (see section 8).
- Permissions subscribe (`entrypoint.py:110-125`): `db:connected`, `vault:ready_for_data`,
  `plugin:settings_changed`, `itsm:catalog_reloaded`, `itsm:record_create|record_update|transition|journal_add|ci_link`,
  `entity_registry:lookup_result`, own broadcasts `itsm:record_updated|state_changed|journal_added`.
  Emit (`:127-140`): `itsm:record_created|record_updated|state_changed|journal_added`, `itsm:*_result`,
  `itsm:ingest_failed`, `itsm:catalog_reloaded`, `entity_registry:register`, `entity_registry:lookup`.
  **`monitoring:outage_persisted` / `monitoring:outage_recovered` are missing from subscribe.**
- REST permissions: `plugin:lyndrix.plugin.itsm:api:read|api:write|itsm:admin` (`app/api/__init__.py:22-25`);
  roles admin/agent/viewer (`entrypoint.py:155-169`).
- Layers: `entrypoint.py` (wiring, 397 lines), `app/logic/service.py` (1063 lines), `app/logic/catalog.py`,
  `app/api/__init__.py`, `app/model/{models,database}.py`, React `app/ui/react/PluginApp.tsx` (993 lines).
  No `app/ui/nicegui/` (React only).
- Settings (`entrypoint.py:98-108`): `attachment_dir`, `max_attachment_mb`, `catalog_dir`. No ITSM rule/monitoring settings.

## 2. Data model

All tables in `app/model/models.py`, SQLAlchemy `_Base` (own declarative base, `:34`).

**`itsm_records`** (`Record`, `models.py:42-120`) - one table for all types:
`uuid` PK (`:50`), `number` unique (`:51`, e.g. `INC0000042`), `record_type` (`:52`), `subtype`, `category` (`:53-54`),
`title` (255), `description` (`:56-57`), `state` (`:59`), `impact`/`urgency` Int (`:60-61`), `priority` String(8)
derived from the matrix (`:62`), `assignment_group`, `assignee`, `requester`, `created_by` (`:64-67`),
`custom_fields` JSON, `tags` JSON (`:69-70`), `opened_at`, `resolved_at`, `closed_at`, `due_at`, `version`
(`:72-76`), gateway fields `source`, `external_id`, `external_url`, `sync_state`, `sync_error` (`:80-84`),
`created_at`, `updated_at`. **Unique `(source, external_id)`** = `uq_itsm_source_ext` (`:44-48`).
There is **no** `severity` field and **no** column for rule/monitor IDs; such data belongs in
`custom_fields` or `tags` (JSON, not indexed).

**`itsm_journal`** (`JournalEntry`, `models.py:123-145`): `record_uuid` FK, `kind`
(`comment | work_note | state_change | field_change | link | attachment | system`, comment `:128-129`),
`author`, `body`, `data` JSON, `created_at`. Append-only (audit trail, `models.py:9-10`).

**`itsm_ci_links`** (`CiLink`, `models.py:148-170`): `record_uuid` FK, `entity_uuid` String(36) (= entity registry UUID = `ci_id`),
`role` String(32), cached `display_name`, `entity_type`; unique `(record_uuid, entity_uuid, role)` (`:150-152`).
Allowed roles: `("affected", "caused_by", "changed", "related")` (`app/logic/catalog.py:46`).
`ci_id` is consistently called `entity_uuid` in ITSM; the API response attaches `ci_links` to the record detail (`service.py:299-340`, `_detail`).

Others: `itsm_record_links` (`models.py:173-192`, relations in `catalog.py:33-43`), `itsm_attachments` (`:195-218`),
`itsm_counters` (`:221-225`). Number allocation `ItsmService._allocate_number` (`service.py:214-246`, own READ COMMITTED tx).
Migrations: `database.py:34` (`_migrate_columns`), DDL under `advisory_lock("lyndrix_bootstrap")` (`database.py:82-87`).

## 3. Catalog

`catalog/record_types/{incident,change,problem,request,task}.yml`, loaded by `CatalogLoader` (`app/logic/catalog.py`;
`service.catalog.load()` in `entrypoint.py:197`, reload `service.py:170-175`).

**Incident** (`catalog/record_types/incident.yml`):
- `prefix: INC`, `number_width: 7` (`:8-9`); `subtypes`: outage, degradation, security, data (`:14-18`);
  categories include `compute` (hypervisor, vm, container), `network`, `storage`, `application`, `monitoring` (`:20-41`).
- **No "severity" field**. Priority = matrix of `impact` (1-3) x `urgency` (1-3) (`:43-68`), default `{impact:3, urgency:3}` (`:52`):
  `1-1 P1; 1-2 P2; 1-3 P3; 2-1 P2; 2-2 P3; 2-3 P4; 3-1 P3; 3-2 P4; 3-3 P4` (`:54-63`);
  levels P1 Critical, P2 High, P3 Medium, P4 Low (`:64-68`). `derive_priority` `catalog.py:185-188`.
- **States** (`:72-78`): `new` (initial, open), `in_progress` (open), `on_hold` (open, requires `hold_reason`),
  `resolved` (kind resolved, requires `resolution_code`, `resolution_notes`), `closed` (final), `cancelled` (final, requires `cancel_reason`).
- **Transitions** (`:80-84`): `new -> in_progress|resolved|cancelled`; `in_progress -> on_hold|resolved|cancelled`;
  `on_hold -> in_progress|cancelled`; `resolved -> closed|in_progress`. A direct `new -> resolved` is allowed.
- Fields (`:88-108`): `resolution_code` select (fixed, workaround, no_fault_found, duplicate, cannot_reproduce), `resolution_notes` text,
  `hold_reason`, `cancel_reason`, `affected_users`, `security_relevant`. Assignment groups: service_desk, infra, network, security (`:110-114`).
- Important: `resolved` sets `resolved_at` and leaves `closed_at` NULL; only `closed|cancelled` sets `closed_at` (`service.py:626-634`).
  "Open" in `list_records` (`include_closed=False`) means `closed_at IS NULL` (`service.py:475-476`), so it **includes** `resolved`.
  A dedupe for "open" must therefore go via the state kind (`open`), not via `closed_at`.

Change type: states from `change.yml:60-68` (among others `assess` requires risk/implementation_plan/rollback_plan, `approval` requires
`planned_start`/`planned_end`, `closed` requires `close_code`) - relevant for the later stage (section 9, item 6).

## 4. Creation and transition paths

**Core**: `ItsmService.create_record(data, author)` (`service.py:392-457`): validates classification (`_validate_classification`, `:342-388`),
`impact`/`urgency` against the catalog, default from the catalog; `priority = rt.derive_priority(...)` (`:421`); initial state (`:420`);
`requester = fields.get("requester") or author`, `created_by = author` (`:423-424`); CI links must already carry `entity_uuid` (`_check_ci_links`, `:664-674`);
`retry_write` on MariaDB 1020/1205/1213 (`:439-443`); journal entry `system`/"created" (`:433`); afterwards
`itsm:record_created {record}` (`:455`) and `entity_registry:register` (`:456`, `_announce_entity` `:1017-1031`).

**REST**: `POST /records` (`api/__init__.py:155-171`), payload `RecordCreate` (`:47-65`). `source`/`external_id` only with `itsm:admin`
(`:163-167`). CI references are resolved before the service (`:169`).

**Bus** (`entrypoint.py`):
- `itsm:record_create` (`:261-277`): payload = `RecordCreate` fields + `author` (default `"bus"`, `:268`) + optional `request_id`;
  **no permission check on the content, `source`/`external_id` are allowed here without an admin check**; reply on `itsm:record_create_result`
  (`:275`/`:277`, via `_reply` `:253-257`). Without `request_id` and on error -> `itsm:ingest_failed` (`:249-257`).
- `itsm:transition` (`:325-343`): `{ref, to_state, fields?, note?, author, version?, request_id?}`, reply `itsm:transition_result`.
- `itsm:journal_add` (`:345-359`): `{ref, kind (default "work_note"), body, author}`; **no reply**, errors only `itsm:ingest_failed`.
  Allowed kinds only `comment|work_note` (`service.py:30`, check `:646-647`).
- `itsm:record_update` (`:306-323`), `itsm:ci_link` (`:279-304`, `ref` + `ci|entity_uuid` + `role`).

**CI resolution**: `ItsmService.resolve_ci` (`service.py:678-719`) -> `ctx.request("entity_registry:lookup", {"ref": ref},
reply_topic="entity_registry:lookup_result", timeout=5.0)` (`:695-698`). Returns `None` if the registry is missing/timeout/`ctx.request` is missing (`:692-702`);
`ItsmError` on unknown/ambiguous (`:703-713`); ITSM records are rejected as a CI (`:714-717`).
`resolve_ci_links` (`:721-750`): a UUID is nevertheless routed through the registry (merge tombstones, name cache, `:740-744`);
without a registry a raw UUID link passes through, a name alone fails (`:745-748`). It is **async** - that fits calls from bus handlers,
`create_record` itself is synchronous and runs via `asyncio.to_thread` (`entrypoint.py:274`).
No `/resolve` REST call from ITSM; the UI uses `registryApi` (`PluginApp.tsx:408`).

**State transition**: `transition()` (`service.py:569-580`) -> `_transition_tx` (`:582-640`): check version (`:587-590`), target state known (`:593-595`),
transition per catalog (`:596-600`), apply `fields` (core fields `assignee|assignment_group|category|subtype|description`, rest = custom, `:603-612`),
check the `requires` of the target state (`:614-621`), timestamps (`:626-634`), journal `state_change` with `note` as body (`:635`), `version+1` via a
conditional UPDATE (`_bump_version` `:247-258`), afterwards `itsm:state_changed` (`:579`). Errors: `TransitionError` (409), `ItsmError` (400), `VersionConflict` (409).

**Resolve/close, system actor**: There is no special resolve function; resolve = `transition(ref, "resolved", fields={"resolution_code": "fixed",
"resolution_notes": "..."}, note=..., author="monitoring")`. `author` is a free string (no user object, no check); the API sets
`author=identity.username` (e.g. `api/__init__.py:170,199`), the bus defaults to `"bus"` (`entrypoint.py:268,336`). A system actor (`"monitoring"`)
is possible without a code change. Audit trail = journal entries with `author`.
**Comment**: `add_journal(ref, "comment"|"work_note", body, author)` (`service.py:644-660`); event `itsm:journal_added`, notification only for `kind == "comment"` (`entrypoint.py:392-393`).

## 5. API routes (mount `/api/plugins/lyndrix.plugin.itsm/`)

| Method + path | Permission | Service | Source |
|---|---|---|---|
| GET `/records` (type, state, priority, assignee, group, tag, ci, q, include_closed, limit, offset) | api:read | `list_records` | `api/__init__.py:133-153` |
| POST `/records` | api:write (source/external_id: itsm:admin) | `create_record` | `:155-171` |
| GET `/records/{ref}` | api:read | `get_record` | `:173-179` |
| PATCH `/records/{ref}` (with `version`) | api:write | `update_record` | `:181-191` |
| POST `/records/{ref}/transition` | api:write | `transition` | `:193-202` |
| POST `/records/{ref}/journal` (`kind` default comment) | api:write | `add_journal` | `:204-210` |
| POST `/records/{ref}/ci-links`, DELETE `.../ci-links/{role}/{entity_uuid}` | api:write | `link_ci`/`unlink_ci` | `:213-232` |
| POST `/records/{ref}/links`, DELETE `.../links/{relation}/{to}` | api:write | `link_records`/`unlink_records` | `:234-248` |
| POST/GET/DELETE `/records/{ref}/attachments[/{uuid}]` | write/read/write | `add/get/delete_attachment` | `:251-284` |
| GET `/catalog`, POST `/catalog/reload` | read / itsm:admin | `catalog.to_payload`, `reload_catalog` | `:287-300` |
| GET `/stats` | api:read | `get_stats` | `:302-305` |

Error mapping: ItsmError 400, NotFound 404, VersionConflict/TransitionError 409, CatalogError 400 (`:111-123`). No `/settings` path (reserved, ok).
`list_records(ci=<entity_uuid>, record_type="incident", state=...)` returns the candidates for dedupe (`service.py:487-488`), but returns full
`to_dict`s, no limit on hits without a `limit` parameter (default 200, sorted `opened_at desc`, `:492`).

## 6. UI components (`app/ui/react/PluginApp.tsx`)

- `PluginApp` (root, `:947-993`): deep links from `window.location.search` (`:956-963`): `?record=<uuid|number>` -> detail, `?new=<type>&ci=<uuid>` -> create,
  `?ci=<uuid>` -> filtered list. Views: list/create/detail/catalog.
- `RecordList` (`:275`, CI filter `:284`, filter chip `:337`), `StatsStrip` (`:258`), `StateBadge` (`:66`, colour by state kind),
  `PriorityBadge` (`:73`).
- `CreateForm` (`:388`): `initialCi` is resolved via `registryApi.get` (`:405-420`), role fixed to `affected`; submit sends `ci_links` (`:426`, `:440`).
- `CiPicker` (`:154`), CI panel in the detail (`:829-847`: list with link "Records for this CI" `:839`, unlink `:843`, picker `:847`).
- `TransitionModal` (`:559`), `RecordDetailView` (`:632`), `CatalogView` (`:900`). API client `app/ui/react/lib/api.ts`.
- A journal entry with `author = "monitoring"` appears in the timeline without a UI change (generic from `itsm_journal`).
- Locales `locales/itsm.{de,en}.json`; new UI texts (source "Monitoring", hint about auto-resolve) would have to be added to both files (i18n parity, `check_i18n_keys.py` in the core).
- Notifications (backend): `ctx.notify` in `_notify` (`entrypoint.py:362-370`); endpoints `record_assigned`, `record_state_changed`,
  `record_commented` (`:142-149`); `state_changed` with severity `warning` for P1/P2 (`:384`). **`itsm:record_created` triggers no notification**
  (no subscriber), i.e. a new incident with assignee `None` produces no bell entry, only `itsm:record_created` on the bus.

## 7. Test status

- `tests/conftest.py` (imports `core.services` before `core.api`, circular-import workaround, `:1-9`); `tests/test_service.py`: 37 tests.
- Fixture `svc` (`test_service.py:25-44`): in-memory SQLite (`StaticPool`, `PRAGMA foreign_keys=ON`), `_FakeCtx` with an `emit()` recorder and `cluster_topics` (`:12-22`);
  `_RegistryCtx`/`_registry()` simulates `ctx.request` (`:341-370`).
- Covered, among others: catalog (`:53`, `:60`), numbers/priority (`:75`, `:90`), transitions/required fields (`:128`), resolve/close/reopen (`:140`),
  CI links (`:115`, `:256`, `:375-418`), `(source, external_id)` uniqueness (`:318`), cluster/retry paths (`:539-707`).
- **Not tested**: bus handlers in `entrypoint.py` (no test imports `entrypoint`), notifications, REST routers except the 409 mapping (`:707`).
- Running (`documentation.md:249-262`): `docker cp . lyndrix-core-dev:/tmp/itsm` and `docker exec -w /tmp/itsm -e PYTHONPATH=/app:/tmp/pytest-sm lyndrix-core-dev python -m pytest -q tests`;
  on the HA bench additionally `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local` (CLAUDE.md). Lint: `ruff check entrypoint.py app tests`.

## 8. Cluster specifics

- No `placement`, no `on_lease`, no `ctx.lease`, no `is_leader` in the plugin (grep over `entrypoint.py` and `app/` empty).
  All instances run routes and bus handlers.
- Existing protection mechanisms: optimistic locking via `version` (`service.py:247-283`), counter tx (`:214-246`), retry on 1020/1205/1213 (`database.py:66-79`),
  bootstrap lock (`database.py:82-87`), catalog reload via `cluster=True` (`service.py:175`), settings re-apply on every instance (`entrypoint.py:223-226`).
- **No guard against duplicate creation by bus events**: a `monitoring:outage_persisted` emitted with `cluster=True` reaches every instance
  (origin marker `payload["_origin"]`, CLAUDE.md); every ITSM instance would run `create_record`. There is no check "open incident per ci_id".
- The only idempotency mechanism: `UNIQUE(source, external_id)` (`models.py:46`) plus a pre-select and IntegrityError mapping (`service.py:411-414`, `:444-449`).
  This reliably catches simultaneous creations with an identical key (also across instances, DB-side).
- Attachments need RWX storage; irrelevant for the function, but `health()` reports `degraded` if the directory is not shared (`service.py:933-1015`).

## 9. Attachment points for plan 7.3

1. **Subscription + permissions** - extend `entrypoint.py:110-125`: `monitoring:outage_persisted`, `monitoring:outage_recovered`
   (optionally `maintenance:override_started`). Create handlers analogous to `_on_record_create` (`:261-277`) in `setup()`; move the logic to `app/logic/`
   (`entrypoint.py` stays thin): a new file, e.g. `app/logic/monitoring_incidents.py`, with the service methods `handle_outage_persisted(payload)` / `handle_outage_recovered(payload)`.
   Risk: the payload schema comes from the monitoring analysis (plan 6/7.2) - fix the fields (`ci_id`, `host_ci_id`, `rule_id`, `outage_id`, `severity`, `auto_resolve`, `since`, `monitor`) in advance.
2. **Create the incident** - use `ItsmService.create_record` directly (not via the bus detour `itsm:record_create`). Fields: `record_type="incident"`,
   `subtype="outage"` (`incident.yml:15`), `category` per CI type (`hypervisor|vm|container|application`, `:23-36`) or `monitoring` (`:40`),
   `title` with the service name, `description` with duration/monitor/rule, `tags=["auto","monitoring"]`, `requester`/`author="monitoring"`.
   CI links in advance via `resolve_ci_links` (`service.py:721`) or, because `ci_id` is already a registry UUID, directly as `{"entity_uuid", "role": "affected"}` (service CI) and
   `{"role": "related"}` (host CI); `resolve_ci_links` calls the registry per UUID with a 5 s timeout (`:740`), and on a registry outage falls back to the raw UUID link (`:745`) - sufficient for alarm paths.
   `_check_ci_links` enforces a 36-character `entity_uuid` (`:667`). Roles per `CI_LINK_ROLES` (`catalog.py:46`), so the host CI is `related` or `affected`.
3. **Severity mapping** - no severity field; a mapping to `impact`/`urgency` is needed so that `priority` comes out of the matrix (`incident.yml:54-63`):
   `critical -> impact 1, urgency 1 (P1)`, `high -> 1/2 (P2)`, `medium -> 2/2 (P3)`, `low -> 3/2 (P4)` (proposal; the matrix yields these values).
   Mapping as a setting (`entrypoint.py:98-108`, `settings_schema` pattern) or as a constant in the new logic file. Allowed values `impact` 1-3 / `urgency` 1-3 (`incident.yml:44-51`, check `service.py:360-373`).
4. **Dedupe (one open incident per ci_id [+ rule])** - does not exist yet. Two variants:
   a) *Idempotency key*: `source="monitoring"`, `external_id=f"{rule_id}:{ci_id}"`; the unique constraint reliably prevents parallel creations, also cluster-wide
   (`service.py:411-414`, `:444-449`). Drawback: the key stays occupied after resolve -> a later new outage would fail with "already exists";
   `external_id` would have to contain the `outage_id` (then dedupe per outage, not per CI) or the old record would have to be reused/reopened
   (`resolved -> in_progress` is allowed, `incident.yml:84`; `closed` is final). In addition, `source` marks the record as externally managed (semantics `models.py:78-79`, REST admin gate `api/__init__.py:163`).
   b) *Query*: new service method `find_open_for_ci(entity_uuid, record_type="incident", tag/rule)`: join `Record` x `CiLink` (`CiLink.entity_uuid == ci`, `role == "affected"`) with
   `Record.state` in open states (state kind `open`, catalog `incident.yml:73-75`) - **not** `closed_at IS NULL` (includes `resolved`, see section 3). Model: `list_records(ci=...)` (`service.py:487-488`).
   Without a DB constraint (b) is not atomic (race between two instances) - hence (b) only with a single writer (item 6) or combined with (a).
   Recommendation to examine: (b) for reopening/comment on repetition ("comment instead of a new incident" via `add_journal`) plus a single writer.
5. **Recovered -> auto-resolve or comment** - find the open incident via the dedupe query, rule choice `auto_resolve` from the payload:
   - true: `transition(ref, "resolved", fields={"resolution_code": "fixed", "resolution_notes": "Automatic: monitor reachable again after <duration>"}, note=..., author="monitoring")`
     (`service.py:569`; required fields `incident.yml:76`). From `on_hold`, `resolved` is **not** allowed (`:83`: only `in_progress|cancelled`) -> the handler must catch `TransitionError` and fall back to a comment.
     An already `resolved` incident stays untouched. Closing (`closed`) remains manual or later via a rule.
   - false: `add_journal(ref, "comment", "...", author="monitoring")` (`service.py:644`). `comment` triggers the notification `record_commented` (`entrypoint.py:392`), `work_note` does not.
   Errors are, following the existing pattern, only logged/`itsm:ingest_failed` (`entrypoint.py:249-251`). Version conflict: without `expected_version` it is retried once (`service.py:260-283`).
6. **Cluster single writer** - missing today. Options: (i) `placement`/`on_lease` (`ctx.on_lease("itsm:monitoring-incidents", acquired=..., lost=...)`, `getattr`-guarded, CLAUDE.md "Cluster mode") - only the lease instance
   processes the monitoring events (the handler checks the `ctx.is_leader` equivalent/lease flag; events with `cluster=True` reach all instances); `ctx.release_lease` not needed. (ii) a short `async with ctx.lease("itsm:outage:<ci_id>")` per event.
   (iii) DB constraint (item 4a). Risk: a lease change between persisted and recovered loses nothing as long as the state lives exclusively in the DB (which is the case, no in-process state).
   Check the manifest permissions for lease topics; core >= 1.7 needed, fallback on an older core: start directly (`getattr` pattern).
7. **System actor / permissions** - `author` is a free string; convention `"monitoring"` (analogous to the default `"bus"`, `entrypoint.py:268`). `requester` falls back to `author` (`service.py:423`).
   The bus path bypasses the `itsm:admin` gate for `source/external_id` (`entrypoint.py:268-274` vs. `api/__init__.py:163`); that is intended for the internal call, but a security risk if other plugins may emit `itsm:record_create` - the core checks `permissions.emit` per plugin.
8. **Notifications** - `itsm:record_created` has no subscriber; for outage incidents (P1/P2), `_notify` (`entrypoint.py:362`) should be extended (new endpoint in `notification_endpoints` `:142-149`, e.g. `record_created`, subscribe to the own `itsm:record_created`
   in `permissions.subscribe`). Without this extension, operators only get a message on assignment/status change/comment. `state_changed` on auto-resolve already notifies (`:381-387`).
9. **UI** - no mandatory change. Optional: tag/badge "auto" and a filter in `RecordList` (`PluginApp.tsx:275`); the deep link `?ci=<uuid>` exists (`:956-963`) and shows all records of a CI (incl. incidents for the maintenance window view).
10. **Second stage `maintenance:override_started` from a change** - the change catalog has `planned_start`/`planned_end` as required fields from `approval` (`change.yml:63`). Hook: a handler on `itsm:state_changed` (`entrypoint.py:381`) with
    `record_type == "change"` and a target state (e.g. `scheduled`/`implement`, look up in `change.yml:60-70`) -> read the CI links with role `changed` (`catalog.py:46`) and emit `maintenance:override_started` (add the topic to `permissions.emit`). Not part of the first iteration.
11. **Add tests** (`tests/test_service.py`): use the fixture `svc`/`_RegistryCtx` (`:25`, `:341`); cases: creation with severity mapping, dedupe (2x persisted -> 1 record), recovered auto_resolve true/false, `on_hold` -> fallback comment, a resolved incident is not resolved again, a new outage after resolve. Handlers in `entrypoint.py` are untested - hence place the logic in `app/logic/`.
12. **Versioning** - manifest `0.2.0 -> 0.3.0` (`entrypoint.py:76`), matching tag (memory: next minor), extend `CHANGELOG.md` and `documentation.md` (German), locales de/en.

## 10. Deviations from the plan / open questions

- Plan 7.3 speaks of "severity from the rule" (`critical|high|medium|low`): ITSM only knows `impact`/`urgency` -> priority matrix P1-P4 (`incident.yml:43-68`). The mapping table must be defined (proposal item 3).
- Plan: "Dedupe: open incident per `ci_id`". Nothing of this exists in the code; "open" is not a catalog filter (`closed_at IS NULL` counts `resolved` too, `service.py:476`). Question: should a `resolved` incident be reused on a new outage (`resolved -> in_progress` possible) or a new one created? Dedupe per `ci_id` alone or per `ci_id` + rule (two rules on one CI)?
- Plan: "service and host CI links": the choice of role is open (`affected` for the service, `related` or `caused_by` for the host?). Roles are fixed to four (`catalog.py:46`); host as cause only makes sense with a root-cause analysis.
- Auto-resolve from `on_hold` is not allowed per the catalog (`incident.yml:83`); should the catalog allow `on_hold -> resolved`, or does the fallback comment apply? The plan says "if the rule allows it, otherwise comment" - the `on_hold` case is not covered.
- Resolve requires `resolution_code` + `resolution_notes` (`incident.yml:76`); for auto-resolve a code must be fixed (proposal `fixed`, possibly a new code `auto_recovered` in the catalog; then a catalog update `incident.yml:96-104`).
- Cluster: plan 6 says "everything fire-and-forget"; whether `monitoring:outage_*` is emitted with `cluster=True` is decided by the monitoring analysis (there lease `monitoring:prober`). If it is emitted only locally and only the prober leader sends, the DB constraint suffices on the ITSM side; otherwise a lease is needed (item 6).
- Coupling rule (CLAUDE.md): "No plugin may *need* another plugin" - ITSM missing -> monitoring must not fail (fire-and-forget, ok); monitoring missing -> ITSM without function, but runnable. A request/reply (`ctx.request`) for the incident number back to monitoring would be optional, the reply topic would have to be in both manifests.
- Missing notification on `itsm:record_created` (section 6) - intended, or to be added?
- `Record` has no reference to an outage/rule: should `custom_fields` get a catalog extension (`incident.yml` `fields`, e.g. `monitor_rule`, `outage_id`) (no code needed, `catalog.py` validates custom fields, `service.py:400`)?
