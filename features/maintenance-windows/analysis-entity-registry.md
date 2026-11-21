# Analysis: Entity Registry (sweep for plan 7.4 / section 10)

Repo: `/home/marvin/gitlab/lyndrix-dev/plugins/lyndrix-plugin-entity-registry` (state: version 0.2.0).
All paths are relative to the repo unless stated otherwise. Read-only analysis, no code changed.

## 1. Overview

| Item | Value | Evidence |
|---|---|---|
| Manifest ID | `lyndrix.plugin.entity_registry` | `entrypoint.py:68` |
| Version | `0.2.0` | `entrypoint.py:75` |
| `min_core_version` | `1.4.0` (no `placement` set, hence default `any`) | `entrypoint.py:83` |
| `auto_enable_on_install` | `True` | `entrypoint.py:84` |
| UI | `react_ui=True`, route `/registry` (sidebar_visible), `i18n_namespace="entity_registry"` | `entrypoint.py:86-92` |
| Roles | `admin` (api:read+write, auto_map `INT_ADMIN`), `viewer` (api:read) | `entrypoint.py:116-125` |
| `permissions.subscribe` | `db:connected`, `entity_registry:{identify,register,lookup,relate,unrelate,retire}`, `entity_registry:catalog_reloaded` | `entrypoint.py:94-103` |
| `permissions.emit` | `entity_registry:{identified,lookup_result,entity_created,entity_updated,entity_merged,entity_retired,conflict,ingest_failed,catalog_reloaded}` | `entrypoint.py:104-114` |
| Layers | `app/model` (models, DB helpers), `app/logic` (catalog, service), `app/api`, `app/ui/react`; no `app/ui/nicegui` | file list, `entrypoint.py:7-15` |
| AI instructions | not present (no `ai/`, no `ai_instructions*` in the manifest) | grep without hits |

The plugin only reads/writes its own DB (MariaDB, SQLAlchemy). It has no background tasks, no leases, no scheduler.

## 2. Data model

### 2.1 Tables (`app/model/models.py`)

| Table | Columns (excerpt) | Line |
|---|---|---|
| `registry_entities` | `uuid` PK(36), `entity_type` String(64) idx, `display_name`, `status` (`active\|retired\|merged`) idx, `owner_source`, `sources` JSON (list "seen by"), `attributes` JSON (dict), `merged_into`, `created_at`, `updated_at`, `last_seen_at` | `models.py:34-67` (entity_type :38, status :41, sources :45, attributes :47, merged_into :49) |
| `registry_identifiers` | `id`, `entity_uuid` FK, `scheme`, `value`, `source`, `created_at`; **UNIQUE (scheme, value)** | `models.py:70-91` (unique :73) |
| `registry_relations` | `id`, `from_uuid` FK, `to_uuid` FK, `relation` String(64), `source`, `created_at`; UNIQUE (from,to,relation) | `models.py:94-119` (unique :97, relation :107) |
| `registry_conflicts` | `entity_type`, `fingerprint`, `open_fingerprint` (UNIQUE index, NULL after completion), `candidates` JSON, `identifiers` JSON, `source`, `status` (`open\|resolved\|dismissed`), `resolution`, timestamps | `models.py:122-` |

There is **no** separate "source records" table and **no** attribute table. "Candidates/merge proposals" = `registry_conflicts` (merge never happens automatically, `service.py:3-7`, `:456-466`). A merge leaves a tombstone (`status="merged"`, `merged_into`); old UUIDs keep resolving (`service.py:145-154`, `:744-745`).

### 2.2 Kind vocabulary (`entity_type`)

- **Definition:** exclusively YAML `catalog/entity_types.yml:14-` (no Python enum). Validated on load via Pydantic (`app/logic/catalog.py:34-39` regex `^[a-z][a-z0-9_]*$`, unique IDs `:57-66`). An unknown type on identify/register → `RegistryError` (`service.py:127-130`).
- **Exact list (7):** `host`, `hypervisor`, `service`, `container`, `network`, `itsm_record`, `generic` (`entity_types.yml:15,19,23,27,31,35,39`).
- **`vm` does NOT exist.** A VM/an LXC is `host` ("Virtual machine or LXC container managed as a server", `entity_types.yml:15-18`); a physical node is `hypervisor`. The orchestrator only sets `host` or `hypervisor` (`lyndrix-plugin-iac-orchestrator/app/controller/engine.py:1189`).
- **Actually used:** orchestrator -> `host`/`hypervisor` (`engine.py:1189,1221`); ITSM -> `itsm_record` (`lyndrix-plugin-itsm/app/logic/service.py:1028`). I found **no** emitter in the plugin directory that creates `service` or `container` entities (grep for `entity_type` with service/container/vm in `plugins/**/*.py` without hits). The vocabulary exists, the data for it does not yet.
- The catalog can be hot-reloaded via `POST /catalog/reload` (`api/__init__.py:264-273`), cluster announce via `entity_registry:catalog_reloaded` (`service.py:118-123`).

### 2.3 Identifier schemes (`entity_types.yml:44-76`)

A lower priority number wins on conflicts: `pve.vm` 10 (format `<node>/<vmid>`), `itsm.number` 10, `bmc.reconid` 10, `iac.host` 20, `fqdn` 30, `docker.container` 40 (`<host>/<name>`), `server_manager.id` 50, `monitoring.monitor` 60, `external` 90. IPs are deliberately **not** schemes (`entity_types.yml:6-9`). An unknown scheme -> `RegistryError` (`service.py:132-136`). Normalisation: trim, empty/None discarded (`service.py:60-68`).

### 2.4 Attributes / extensibility

- `Entity.attributes` is **a single JSON column** `dict` (`models.py:47`), "Weak / descriptive facts ... Never used for matching" (`models.py:46`).
- **No namespacing per source.** Keys are flat and free; every writer (identify/register/PATCH) can set any key. `sources` is a separate list, not per attribute (`service.py:512-514`).
- **Write paths:**
  - `identify`/`register` -> `_absorb` or `_create`: **flat merge** `{**old, **new}`, new values overwrite (`service.py:507-511`); tagged `changes` -> `entity_updated`.
  - `PATCH /entities/{uuid}` -> `update_entity(attributes, replace_attributes)`: merge or replace (`service.py:668-672`, `api/__init__.py:110-124`), permission `api:write`.
  - Bus: `identify` with `attributes` (`entrypoint.py:188-195`).
  - Merging two entities: `{**drop, **keep}`, keep wins (`service.py:738`).
- **Read:** `GET /entities/{uuid}` -> `{"entity": detail}` with `attributes` as a dict (`models.py:60` `to_dict`, `service.py:156-190`, `api/__init__.py:102-108`). `GET /entities` (list) returns the same `to_dict()` fields + `identifiers`, **no** relations (`service.py:233-236`).
- Typical production example: the orchestrator sends `ip, site, stage, groups` as attributes (`engine.py:1213-1219`).
- No schema/whitelist/size limit on `attributes` (Pydantic `Dict[str, Any]`, `api/__init__.py:31`).

### 2.5 Relations

- **Yes, a relation model exists:** `registry_relations` (directed, labelled), catalog labels `runs_on`/`hosts`, `depends_on`/`required_by`, `member_of`/`has_member`, `affects`/`affected_by`, `related_to` (`entity_types.yml:77-102`). Inverses are only declared in the catalog (`catalog.py:94-98`); **only one direction** is stored (`service.py:778`), `_entity_detail` returns incoming and outgoing edges with `direction` + `other_uuid/other_display_name/other_entity_type` (`service.py:166-189`).
- Writing: `relate()` (`service.py:761-785`; bus `entity_registry:relate` `entrypoint.py:275-293`; REST `POST /entities/{uuid}/relations`).
- **Fact:** no plugin in the repo tree calls `entity_registry:relate` (grep for `entity_registry:relate` outside the registry repo without hits). The relations table is therefore probably empty in operation; `runs_on` service->host is maintained by nobody today. The orchestrator publish path (`engine.py:1192-1260`) only does `identify` for hosts.
- Consequence: a consumer finds the host CI of a service CI **only** if someone creates `service --runs_on--> host`. Alternative hints: `attributes` (free, e.g. `site`, `stage`) and the `docker.container` identifier `<host>/<name>` (the host part is an `iac.host` key, `entity_types.yml:62-65`).

## 3. Contracts

### 3.1 `entity_registry:identify` -> `entity_registry:identified` (`entrypoint.py:197-226`)

Request (single): `{entity_type, identifiers:{scheme:value}, display_name?, attributes?, source, request_id?}` (Pydantic `IdentifyPayload`: `entity_type`, `identifiers` min 1, `source` 1-64 required; `api/__init__.py:27-32`).
Request (batch): `{items:[IdentifyPayload...], request_id?}` (batch items are not limited to 1000 on the bus path, REST `max_length=1000`, `api/__init__.py:36`).
Reply only if `request_id` is set (`entrypoint.py:223-226`, otherwise `entity_registry:ingest_failed` on error):
- Single: `{request_id, ok:true, uuid, created:bool, conflict:bool}` (`entrypoint.py:194-195`)
- Batch: `{request_id, ok:true, results:[{ok:true, uuid, created, conflict} | {ok:false, error, identifiers}]}` (`entrypoint.py:207-216`), order = request order (the orchestrator uses `zip(hosts, results)`, `engine.py:1237`).
- DB not ready: `{request_id, ok:false, error:"database not ready"}` (`entrypoint.py:205-206`).
Semantics (`service.py:377-466`): find-or-create. No hit -> new (`created:true`). Exactly one hit -> incorporate aliases/name/attributes/source (`_absorb`). Several -> `conflict:true`, the returned UUID = entity with the strongest scheme (`:457-460`), only the conflict record is written. A type mismatch is only logged (`:442-447`).
Call pattern of the consumers: `ctx.request("entity_registry:identify", {"items": items}, reply_topic="entity_registry:identified", timeout=10)` (`engine.py:1225-1228`); `ok=false`/`None` = registry missing -> continue without `ci_id` (`:1229-1231`).

**Important for maintenance:** `identify` **creates entities**. Whoever wants to emit an event only if the `ci_id` exists should use `lookup`/`resolve` (read-only), not `identify`.

### 3.2 `entity_registry:lookup` -> `entity_registry:lookup_result` (`entrypoint.py:246-273`)

Request: `{ref | uuid | (scheme + value), entity_type?, request_id}`; `scheme`+`value` become `ref="scheme:value"` (`:255-257`).
Reply (only with `request_id`): `{request_id, ok:true, entity: detail|null, match: "uuid"|"identifier"|"display_name"|"ambiguous"|null, candidates:[summary...]}` (`:267`, `service.py:251-312`). Errors: `ok:false, error` ("ref ... required", "database not ready", `:259-261`).
`resolve()` resolution of the `ref` (`service.py:269-312`):
1. UUID regex -> follow tombstones; `entity_type` filter optional; otherwise `entity:null`, `candidates:[]` (`:270-274`).
2. `scheme:value`, **only** if the prefix is a catalog scheme (so that FQDN/`hydra/8003` are not split wrongly, `:276-282`); otherwise the bare value against all schemes.
3. Hits only with `status=="active"` (retired/merged targets are discarded, `:286-287`). 1 hit -> `match:"identifier"`; >1 -> `entity:null`, `match:"ambiguous"`, max. 5 `candidates` (`:291-296`).
4. Display name case-insensitive, same 1/>1 logic (`:298-309`).
5. Otherwise `entity:null, match:null`, `candidates` = substring hits (`:311-312`).
`entity` is the full `_entity_detail` (incl. `identifiers`, `relations`, `attributes`).

### 3.3 REST `GET /resolve?ref=&type=` (`api/__init__.py:202-212`)
Always 200 with the same body as `resolve()`; `ref` 1-255 characters, permission `api:read`. `GET /lookup?scheme=&value=`, in contrast, returns 404 on no hit (`:192-200`).

### 3.4 Other bus contracts
- `entity_registry:register` (BYO UUID, idempotent, foreign-owned identifiers -> error) `entrypoint.py:228-244`, `service.py:565-637`. Used by ITSM.
- `relate`/`unrelate`/`retire`: `entrypoint.py:275-317`; errors only via `ingest_failed` or log, **no reply events**.

## 4. Events

| Direction | Topic | Payload | Evidence |
|---|---|---|---|
| emit | `entity_registry:entity_created` | `{entity}` (detail incl. identifiers, relations) | `service.py:433,595` |
| emit | `entity_registry:entity_updated` | `{entity, changes:[...]}`; tags: `identifier:<scheme>`, `display_name`, `attributes`, `sources`, `status`, `type_mismatch`, `relations` | `service.py:451,597,656,784` |
| emit | `entity_registry:entity_merged` | `{keep, drop_uuid}` | `service.py:700` |
| emit | `entity_registry:entity_retired` | `{entity}` | `service.py:689` |
| emit | `entity_registry:conflict` | `{conflict}` (only on first creation) | `service.py:464` |
| emit | `entity_registry:ingest_failed` | `{topic, error, payload_keys}` | `entrypoint.py:185` |
| emit (cluster) | `entity_registry:catalog_reloaded` | `{reloaded_at}` | `service.py:123` |
| emit | `identified`, `lookup_result` | replies (see 3) | `entrypoint.py:224,271` |
| subscribe | `db:connected`, `identify`, `register`, `lookup`, `relate`, `unrelate`, `retire`, `catalog_reloaded` | | `entrypoint.py:166-170,197-308` |

Notes:
- **There is currently no subscriber on `maintenance:*`.** New topics must be listed in `permissions.subscribe` (`entrypoint.py:94-103`).
- Emits from `to_thread` workers are marshalled back to the loop (`service.py:829-847`).
- `entity_updated` is also emitted for pure attribute changes (`service.py:656`) and contains the **complete** entity detail -> a maintenance write would produce a full detail event per entity (mind the volume for `windows_published` with many items, see 9).
- All bus handlers are local (only the instance that receives the event); only `catalog_reloaded` is sent with `cluster=True` and processed by peers (`payload["_origin"]`) (`entrypoint.py:170-179`).

## 5. API routes

Prefix `/api/plugins/lyndrix.plugin.entity_registry/` (`api/__init__.py:3`). All handlers: `_ensure_ready()` (503 if tables are not ready) and `asyncio.to_thread`; `RegistryError`->400, `NotFound`->404 (`api/__init__.py:72-85`). Permissions `plugin:<id>:api:read|write` (`:21-22`).

| Method | Path | Perm | Line |
|---|---|---|---|
| GET | `/entities?type=&q=&status=active\|retired\|all&limit(1-1000,200)&offset` | read | :88-100 |
| GET | `/entities/{uuid}` -> `{entity}` (follows tombstones) | read | :102-108 |
| PATCH | `/entities/{uuid}` (`display_name`, `attributes`, `replace_attributes`) | write | :110-124 |
| POST | `/entities/{uuid}/retire` | write | :126-134 |
| POST | `/entities/{uuid}/merge` (`drop_uuid`) | write | :136-144 |
| POST | `/identify` | write | :147-155 |
| POST | `/identify/batch` (max 1000) | write | :157-179 |
| POST | `/register` | write | :181-190 |
| GET | `/lookup?scheme=&value=` (404 on no hit) | read | :192-200 |
| GET | `/resolve?ref=&type=` | read | :202-212 |
| POST | `/entities/{uuid}/relations` (`to_uuid`, `relation`) | write | :215-225 |
| DELETE | `/entities/{uuid}/relations/{relation}/{to_uuid}` | write | :227-237 |
| GET | `/conflicts?status=` | read | :240-245 |
| POST | `/conflicts/{id}/dismiss` | write | :247-257 |
| GET | `/catalog` | read | :260-262 |
| POST | `/catalog/reload` | write | :264-273 |
| GET | `/stats` | read | :275-278 |

No route uses `/settings` (reserved path). No batch read route (no `GET /entities?uuids=`), no `settings_schema`.

## 6. UI components (`app/ui/react/PluginApp.tsx`, 562 lines)

| Component | Line | Content |
|---|---|---|
| `TypeBadge` | :41-49 | Type badge from the catalog (icon/label) |
| `StatusBadge` | :51-60 | `active/retired/merged` (`lx-badge--up/paused/muted`) |
| `IdentChip` | :62-68 | Identifier chip |
| `StatsStrip` | :110-131 | Counters per type, conflict button |
| `EntityList` | :133-225 | Table name/type/identifiers/seen by/last seen. **Type column** :207 (`<TypeBadge/> + StatusBadge` if not active) |
| `EntityDetail` | :227- | Header row :279-288 (`h2` + `TypeBadge` + `StatusBadge`); Identity card :292-315; **Attributes** card :317-332 (renders all `attributes` as a key/value table, non-strings via `JSON.stringify` :327); Relations :334-365; ITSM card :367-393; Actions :395- |
| `ConflictList` | ~:455-515 | Conflict list/merge |
| `PluginApp` (root) | :508-562 | Deep link `?uuid=<36 characters>` -> detail view :523-528 |

Types: `Entity.attributes: Record<string, unknown>` (`app/ui/react/lib/api.ts:36`), `Entity` interface from :27. API client `registryApi.listEntities` :124, `getEntity` :133. No `panel` prop support, no panels.
The built bundle `app/ui/static/ui_bundle.js` is tracked in git (build via `vite.ui.config.ts`) -> rebuild and commit it after a UI change.
i18n: `locales/entity_registry.{de,en}.json`, plus `defaultValue` fallbacks in the code (e.g. `PluginApp.tsx:57`).

## 7. Test status

- `tests/test_registry.py` (474 lines, ~30 tests) against **in-memory SQLite** with `PRAGMA foreign_keys=ON` and `StaticPool`; fixture `svc` builds `EntityRegistryService` + `_FakeCtx` (collects `events`, `cluster_topics`) (`tests/test_registry.py:12-55`).
- Covered: identify (new/recognised/rename via stronger identifier/attribute merge/conflict/validation), merge + tombstone, register idempotency, relations, retire/resurrect, list/search/stats, catalog duplicates, `resolve` (UUID/scheme/name/ambiguous/tombstone/unknown), bootstrap lock + race, retry on 1020, identify insert race, `last_seen` throttling, unique open conflict, catalog reload cluster-wide, health probe.
- **Not covered:** the bus handlers in `entrypoint.py` (`_on_identify`, `_on_lookup`, ...), the REST routers (`app/api`), the React UI. No test for attribute concurrency.
- `tests/conftest.py:9` imports `core.services` before `core.api` (circular import, `TODO(agent)` in the core).
- Running: `pip install -r requirements-dev.txt` (pytest, pytest-cov, mypy, ruff, black); `pytest` from the repo root; the core must be on the path (workspace rule: in the `lyndrix-core-dev` container via `docker cp` + `PYTHONPATH=/app`, on the cluster bench `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local`; `lyndrix-dev/CLAUDE.md`).

## 8. Cluster specifics

- No lease, no `placement`, no single writer. Docstring: "Routes and bus handlers run on every instance; all state lives in the shared DB" (`entrypoint.py:52-57`).
- Table bootstrap under `db_instance.advisory_lock("lyndrix_bootstrap")` (`getattr`-guarded), "already exists" (1050/1060/1061) counts as success (`app/model/database.py:4-5,89-112`); a failed init is retried up to 5 times (`service.py:93-105`).
- Sessions READ COMMITTED (`database.py:173-185`); writes via `retry_write` on 1020/1205/1213 (`database.py:73`, `service.py:358-375`) plus **one** re-run after `IntegrityError` ("lost insert race" -> the lookup sees the winner row and absorbs into it). This is the mechanism against duplicate identities: **UNIQUE(scheme,value)** in the DB (`models.py:73`) + re-read; additionally UNIQUE `open_fingerprint` against duplicate conflicts (`service.py:552-557`).
- `last_seen_at` is written at most every 30 s (`service.py:42,520-521`) to avoid hot-row collisions.
- The **CorrelationStore arbiter mentioned in the briefing does not exist in this plugin**; `CorrelationStore` is a core building block (`lyndrix-core/app/core/components/messaging/logic/correlation.py`, exported `core/api/__init__.py:277`) for interactive actions, unrelated here.
- **Gap for JSON attributes:** `_absorb`/`_update_tx` do a read-modify-write of the **whole** `attributes` column (`service.py:508-510,669-672`). Two instances that simultaneously write different keys of the same entity can overwrite each other (last write wins; READ COMMITTED does not prevent this, no row lock, no version column).

## 9. Attachment points for plan 7.4

Goal per the plan: an attribute `maintenance` on the CI (current/next window, source, override) as a read model, fed from `maintenance:windows_published` / `override_started` / `override_ended`, visible in `GET /entities/{id}` (plan §7.4, §6).

### 9.1 Storage: own table instead of an `attributes` key (recommendation)

Facts against using `attributes["maintenance"]`:
1. No namespacing; `identify`/`PATCH` with `attributes` can overwrite the key (`service.py:508`), as can `replace_attributes=True` (`:669`).
2. Whole-column RMW race (section 8) with two sources (orchestrator windows + monitoring overrides) on the same entity.
3. Every change triggers `entity_updated` with full detail (`service.py:656`).
4. Windows must be kept per **source** (plan §6: `source`, `replaces:true`, several emitters: orchestrator, monitoring), overrides per `override_id` with `until` - that is multi-row.

Proposal (new file, additive): `app/model/models.py` -> table `registry_maintenance` with `id`, `entity_uuid` (FK, idx), `kind` (`window`\|`override`), `source` (String 64), `override_id` (nullable, UNIQUE with `entity_uuid`), `windows` JSON (only for `window`), `until` DateTime (override only), `reason`, `updated_at`; UNIQUE(`entity_uuid`,`source`,`kind`,`override_id`). `create_all` creates new tables idempotently (`database.py:105,110`), no `_NEW_COLUMNS` entry needed (that is only for new columns of existing tables, `database.py:34-37`).
Minimal alternative: JSON column `maintenance` on `registry_entities` (new column via `_NEW_COLUMNS["registry_entities"]`, `database.py:34`), but then the RMW risk remains, only sensible with a row lock/`SELECT ... FOR UPDATE`.

### 9.2 Bus handlers
- File `entrypoint.py`: three new `@ctx.subscribe("maintenance:windows_published" | "maintenance:override_started" | "maintenance:override_ended")` in the style of `_on_relate` (`entrypoint.py:275-293`): validate the payload, check `svc.is_ready`, do the work via `asyncio.to_thread`, errors via `_fail` (`:182-186`).
- Extend `permissions.subscribe` with the three topics (`entrypoint.py:94-103`); otherwise the core does not deliver them. Optional `emit`: `entity_registry:entity_updated` already exists.
- File `app/logic/service.py`: new methods `apply_windows(source, items, replaces)`, `start_override(override_id, ci_ids, until, reason, source)`, `end_override(override_id)`, read helper `maintenance_for(s, uuids)`; integrate into `_entity_detail` (`service.py:156-190`, field `data["maintenance"]`) and `_summaries`/`list_entities` (`:233-236`, `:314-320`) via **one** IN query (no N+1; lists go up to limit 1000).
- Mapping `ci_id` -> entity: use `_resolve_live(s, uuid)` (follows tombstones, `service.py:145-154`), silently ignore an unknown `ci_id` (plan §2.5 "degrade"). **Do not** create an entity via `identify`.
- Semantics of `replaces:true`: per `(source)` delete all previous `window` rows and write anew, in **one** transaction via `self._write(...)` (`service.py:358-375`) - retry/IntegrityError handling comes for free.
- "Next window": the plugin needs the time evaluation. Plan §5 promises a core library (API 1.13); until then store only the raw schedules and compute `current/next` on read (not on write, otherwise the value goes stale without a timer). There is no scheduler in the plugin - an expired override must likewise be hidden on read via `until < now`, because `override_ended` can get lost (fire-and-forget).

### 9.3 Cluster safety of the bus writes
- Handlers run only on the instance that receives the event (local bus delivery, `entrypoint.py:52-53`). If the orchestrator (control plane runs on every instance) emits with `cluster=True`, **all** registry instances receive the event and write the same rows -> must be idempotent (UNIQUE key + `delete`+`insert` in one transaction, retry on 1020/1205/1213, `IntegrityError` = "a peer was faster"). If it emits locally, the event is lost if the registry were only "active" on another instance - since the plugin is `placement=any`, every instance is the recipient of its local event; the source of state is the DB, so local handling suffices. Open decision: which variant the emitter uses (plan §6 says nothing about it).
- No lease needed as long as the writes are idempotent.

### 9.4 REST/UI
- `GET /entities/{uuid}` (`api/__init__.py:102-108`) and `GET /entities` (`:88-100`) return the field `maintenance` automatically once it is added in `_entity_detail` / the list dict; proposed shape: `{"active": bool, "current": {...}|null, "next": {"start":..}|null, "override": {"id","until","reason","source"}|null, "sources": [...]}`.
- Optional filter `GET /entities?in_maintenance=true` (`service.list_entities`, `:198-236`) and batch reading for consumers (`GET /maintenance?ci_ids=...`), since today only a single GET exists. New route under `/maintenance` (not `/settings`).
- Optional bus event `entity_registry:entity_updated` with `changes:["maintenance"]` (consumers know the tag schema, `service.py:451,597,656`); on mass publish better not to emit per entity (volume: full detail per event).
- UI badge: `app/ui/react/PluginApp.tsx`:
  - List: in the type cell next to `StatusBadge` (:207) - new `MaintenanceBadge` (e.g. `lx-badge--paused`, icon `build`/`engineering`, title = source/end).
  - Detail: header row next to `StatusBadge` (:285) and a new "Maintenance" card (current/next window, override with end, source) between Attributes/Relations and the ITSM card (:367).
  - Extend type `Entity` in `lib/api.ts` (:27ff) with `maintenance?`; strings in `locales/entity_registry.{de,en}.json` (`check_i18n_keys.py` checks parity).
  - Rebuild the bundle (`app/ui/static/ui_bundle.js` is tracked).
  - If panels are used later: `PluginApp` has no `panel` prop (`PluginApp.tsx:508`).

### 9.5 Host CI of a service CI
- Today: a relation `service --runs_on--> host` would be the clean way (`entity_types.yml:78-83`, `service.py:761-785`), but it is maintained by **no** plugin. A consumer (monitoring/ITSM) can read it via `GET /entities/{id}` (`relations[]` with `direction`, `other_uuid`, `other_entity_type`, `service.py:178-189`) or `resolve`.
- There is no service emitter (section 2.2). For plan §4.2 inheritance service->host to work in the registry, it needs (a) an emitter that identifies services as `entity_type: "service"` and triggers `entity_registry:relate` (`runs_on`) (the orchestrator publish, `engine.py:1192-1260`, is the natural place), or (b) the orchestrator delivers `host_ci_id` directly in the event/inventory (the plan mentions `monitoring:outage_persisted` with `host_ci_id?`, §6). Recommendation: (b) as the mandatory path, (a) additionally, so the registry remains the truth.
- Optional helper field in the registry: `GET /entities/{uuid}/host` (returns the host via `runs_on`) - only sensible if (a) is implemented.

### 9.6 Kind vocabulary vs. plan
Plan §4.2 (`incident_defaults`) and §6 use `service | host | vm | container`; the registry has `service, host, hypervisor, container, network, itsm_record, generic` (`entity_types.yml`).
- `vm`: **not present**; VMs/LXC are `host`. The plan schema `kind: host|vm|service` (§6) thus yields a value that is not a registry type. Either adapt the plan (drop `vm` or add `hypervisor`: physical nodes are `hypervisor`, `engine.py:1189`) or add a type `vm` to the catalog (costly: the orchestrator would have to identify `vm` instead of `host`, otherwise the type mismatch is only logged, `service.py:442-447`; existing entities keep their type).
- `hypervisor` is missing in the plan; `incident_defaults` should at least know `hypervisor` (plan "types = entity registry kinds", §9.4).
- The catalog is hot-reloadable (`POST /catalog/reload`), but there is **no** mechanism for other plugins to extend the vocabulary; only a repo change to `catalog/entity_types.yml`. For reading: `GET /catalog` (`api/__init__.py:260-262`) - monitoring can validate the valid types for `incident_defaults` against it.

## 10. Deviations from the plan / open questions

1. **`vm` is not a registry kind** (see 9.6). Decision needed: normalise the plan to `host|hypervisor|service|container` (recommended, without a registry change) or extend the catalog.
2. **No service emitter, no relations in operation** (see 2.5, 9.5). Plan §4.2 "inheritance service -> host" cannot be represented via the registry today; `host_ci_id` must come from the emitter or the orchestrator must create `service` entities + `runs_on`.
3. **`maintenance` as an `attributes` key is unsafe** (no namespace, RMW race, event noise, see 9.1). The plan says "attribute"; an own table is recommended, still exposed as the field `maintenance` in `GET /entities/{id}`.
4. **Plan §6 "registry" as a consumer of `windows_published`**: items carry `kind: host|vm|service`; the registry should ignore `kind` and use only `ci_id` (the type is already stored there) to avoid the `vm` conflict.
5. **Read-time evaluation** (current/next, expired overrides) instead of write-time, because the plugin has no timer and events can be lost (see 9.2). Depends on the core time model (plan §5, API 1.13).
6. **Cluster delivery of the `maintenance:*` events** (local vs. `cluster=True`) is not specified in the plan (see 9.3).
7. **Event noise:** should the registry emit `entity_updated` (with full detail) on maintenance changes? Recommendation: no per-entity event on mass publish; at most a collective event or none at all.
8. **Authorisation/provenance:** events on the bus are unchecked; any plugin with `emit` permission on `maintenance:*` can set windows. The registry should store `source` per row and must not allow deletions of foreign sources that depend on the payload (`replaces` only within the same `source`).
9. **Retired/merged target entities:** `_resolve_live` follows tombstones, `status=retired` stays resolvable (`service.py:145-154`) - decide whether windows are stored for retired CIs (proposal: yes, but do not show them in lists).
10. **Docs:** `documentation.md` (German) must be extended when adding the table, route and events (table overview `documentation.md:31`, bus table :110ff); `CHANGELOG.md` + version/tag bump per workspace convention (next minor, manifest + tag, `entrypoint.py:75`).
11. **Tests:** new tests in the style of the `svc` fixture (`tests/test_registry.py:25-55`) for `apply_windows` (replaces per source), override start/end/expiry, unknown `ci_id`, tombstone resolution, idempotency on duplicate delivery; the bus handlers are so far untested - add with `_FakeCtx`.
