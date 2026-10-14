# Cluster Component

Coordination layer for N core instances — and the single-instance no-op it degrades to.
Operator documentation lives in the [High Availability](../ha/index.md) section; this page is the
component reference.

## Files

| Path | Role |
|---|---|
| `core/components/cluster/logic/cluster_service.py` | `ClusterService` singleton: instance id/roles, coordinator lifecycle, leader tracking, readiness, preflight cache, restart fan-out, snapshot/describe |
| `core/components/cluster/logic/coordinator.py` | `Coordinator` protocol, `InstanceInfo`, `LeaseInfo`, factory |
| `core/components/cluster/logic/drivers/local.py` | single mode: leases always granted, in-memory kv, publish no-op |
| `core/components/cluster/logic/drivers/db.py` | shared MariaDB: `cluster_instances/leases/kv/events`, READ COMMITTED + retry on 1020/1205/1213, event poll loop, pruning |
| `core/components/cluster/logic/models.py` | the four tables (SQLAlchemy, shared `Base`) |
| `core/components/cluster/logic/leases.py` | `LeaseKeeper`: wanted leases, acquire/renew ticks, acquired/lost callbacks, fencing after TTL, release broadcast |
| `core/components/cluster/logic/bridge.py` | bus ↔ coordinator bridge: allowlist `CLUSTER_TOPICS`, `cluster=True`, `_origin` marker |
| `core/components/cluster/logic/preflight.py` | the contract checks |
| `core/components/cluster/logic/drain.py` | SIGTERM drain controller |
| `core/components/cluster/api/cluster_api.py` | `GET /api/live`, `GET /api/ready`, `GET /api/cluster`, `GET /api/cluster/preflight`, `POST /api/cluster/leases/{name}/release` |

## Lifecycle

| Event | Action |
|---|---|
| FastAPI startup | `on_startup()`: log mode/roles, install the drain handler when `LYNDRIX_DRAIN_SECONDS > 0` |
| `db:connected` (first) | coordinator start (schema under `GET_LOCK('lyndrix_bootstrap')`), bridge attach, lease keeper start, want `core:leader` if role `leader`, first tick synchronously, heartbeat loop; on failure retry with back-off |
| `system:boot_complete` | run the preflight (also after coordinator start, on membership changes, every 5 min) |
| `cluster:lease_released` (bridged) | kick the keeper → acquire now |
| `system:restart_requested` (bridged) | cluster mode: staggered SIGTERM to self (leader last) |
| SIGTERM | drain → close SSE → `stop()` (release leases, deregister) → uvicorn shutdown |
| FastAPI shutdown | `stop("shutdown")` (idempotent) |

Emitted topics (local, forwarded to authenticated SSE clients): `cluster:changed`
(`reason`: started, start_failed, leader, lease, instances, preflight), `cluster:leader_changed`,
`system:draining`. Bridged: `cluster:lease_released`.

## Bus changes

`GlobalEventBus.emit(topic, payload, *, cluster=False)` calls the bridge's publish hook after the
local dispatch; `set_cluster_publisher()` installs it. `ModuleContext` gains `instance_id`,
`is_leader`, `cluster_mode`, `cluster_size`, `roles`, `on_lease()`, `holds_lease()`, `lease()`
and the `cluster` flag on `emit()`; `dispose()` forgets leases. `ModuleManifest.placement`
(`any | singleton | pinned:<role>`) is applied by the `ModuleManager` (`remote` status + 503 stub
via `router_registry.register_remote`).

## Other components touched

- Database: `advisory_lock()`, `pool_recycle`, endless reconnect with `db:reconnected`.
- Auth: bootstrap under the lock, IntegrityError-tolerant seeds, OIDC state / SSO codes / login
  limiter in the cluster kv, OIDC callback registered without NiceGUI.
- Plugins: tolerant `plugin_states` inserts, `plugin:desired_changed` + reconcile, `remote`
  handling, `PluginOut.placement`.
- Boot: cluster rows in the snapshot; ignores the repeated `iam:ready` after a DB reconnect.
- Events API: `close_all_streams()`, `cluster:*` topics; health endpoint cached 10 s with
  `ready`/`instance_id`; system restart fan-out; settings propagation (`settings:changed`).

## Tests

`tests/cluster/` — coordinator on SQLite with two instance ids, lease keeper transitions, bridge,
context API, placement, preflight and readiness. Run inside `lyndrix-core-dev` with
`LYNDRIX_CLUSTER_MODE=single LYNDRIX_COORDINATOR=local` when the container itself runs in cluster
mode.
