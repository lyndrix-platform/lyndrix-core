# Configuration reference

All names follow the existing prefixes. Helm values and IaC variables are only transport for
exactly these values — Compose and Kubernetes operators share one vocabulary.

| Variable | Default | Tier | Meaning |
|---|---|---|---|
| `LYNDRIX_CLUSTER_MODE` | `single` | 1 | `single` \| `cluster`. `cluster` turns on the `db` coordinator, preflight, drain default 10 s |
| `LYNDRIX_INSTANCE_ID` | `<hostname>-<4 hex>` | 1 | stable id for diagnostics and lease holder names; a new id after a restart is harmless |
| `LYNDRIX_INSTANCE_ROLES` | single: `api,leader,runner` · cluster: `api,leader` | 1 | roles of this instance (see [Architecture](architecture.md#instances-and-roles)) |
| `LYNDRIX_COORDINATOR` | single: `local` · cluster: `db` | 1 | coordinator driver; `cluster` + `local` is rejected at startup |
| `LYNDRIX_LEASE_TTL_SECONDS` | `30` | 1 | a lease not renewed within the TTL is up for grabs — this is the failover time for a dead instance |
| `LYNDRIX_LEASE_HEARTBEAT_SECONDS` | `10` | 1 | heartbeat / renewal interval; must fit at least twice into the TTL |
| `LYNDRIX_EVENT_POLL_MS` | `500` | 1 | driver `db`: poll interval of `cluster_events` (≥ 100) |
| `LYNDRIX_TRUSTED_PROXIES` | `127.0.0.1` | 0 | exported as uvicorn `FORWARDED_ALLOW_IPS` by `docker/entrypoint.sh`; set to the load balancer address(es) or `*` |
| `LYNDRIX_DRAIN_SECONDS` | single `0` · cluster `10` | 0/1 | SIGTERM → `/api/ready` 503 → wait → close SSE → release leases → exit |
| `LYNDRIX_UI_ENGINE` | `api` | 1 | run `react` behind a load balancer; `nicegui` needs sticky sessions (preflight warns) |
| `DB_HOST` | `db` | 0 | the **single-writer endpoint** (MaxScale/ProxySQL/Service); the preflight warns when it is a Galera node addressed directly |
| `DB_ALLOW_DIRECT_NODE` | `false` | 2 | silence that warning on purpose |
| `DB_POOL_RECYCLE_SECONDS` | `300` | 1 | recycle pooled connections before a proxy drops them (0 = never) |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `60` | 0 | per-connection statement cap (API 1.5) |
| `VAULT_URL` | `http://vault:8200` | 0 | load-balanced or active Vault endpoint |
| `VAULT_NODES` | — | 2 | comma-separated list of every Vault node; today only health-checked by the preflight |
| `LYNDRIX_MASTER_KEY` + `SECURITY_DIR/vault_keys.enc` | — | 0 | unchanged; in HA the key file is a shared secret on every instance |
| `STORAGE_SECRET` | — | 0 | unchanged; **must be identical on every instance** (preflight compares fingerprints; a mismatch keeps the instance out of rotation) |

Planned but not shipped (documented so the names are reserved): `LYNDRIX_MIGRATE_MODE`,
`LYNDRIX_PLUGIN_ARTIFACTS`, `LYNDRIX_STORAGE_DRIVER` / `LYNDRIX_STORAGE_S3_*`, `VAULT_UNSEAL_MODE`,
`VAULT_AUTH`, `LYNDRIX_REDIS_URL`.

## Preflight rules

The preflight runs after boot, whenever the set of live instances changes, every five minutes,
and on demand (`GET /api/cluster/preflight`, Settings → Cluster → *Re-run checks*). Each check is
`ok`, `warn`, `fail` or `skip`:

| Check | fail | warn |
|---|---|---|
| `coordinator` | cluster mode and the coordinator is not connected | — |
| `redundancy` | — | only one live instance |
| `storage_secret` | `STORAGE_SECRET` fingerprints differ between live instances | — |
| `versions` | — | mixed core versions (expected during a rolling update) |
| `plugin_artifacts` | — | the loaded plugin set differs between instances |
| `roles` | a `pinned:<role>` plugin has no live instance with that role; no leader-capable instance | — |
| `ui_engine` | — | NiceGUI enabled in cluster mode |
| `trusted_proxies` | — | `X-Forwarded-For` arrives (or cluster mode) while `LYNDRIX_TRUSTED_PROXIES` is the default |
| `database` | — | `DB_HOST` is a Galera node (`wsrep_on`) and `DB_ALLOW_DIRECT_NODE` is not set |
| `clock` | — | this instance's clock is more than 5 s off the database clock |
| `vault_nodes` | — | a node in `VAULT_NODES` does not answer `/v1/sys/health` |
| `drain` | — | cluster mode with `LYNDRIX_DRAIN_SECONDS=0` |
| `storage` | — | cluster mode: `STORAGE_DIR` is not one shared mount (peer marker files missing/stale), or only one instance is alive so it cannot be verified |

Only `coordinator` and `storage_secret` failures take **this** instance out of rotation
(`/api/ready` → 503 with `preflight:<id>` in `reasons`). A cluster-level failure such as a missing
runner instance is shown red on the Cluster page and in the logs but does not stop every instance
from serving traffic.

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/live` | none | process is up (liveness probe) |
| `GET /api/ready` | none | 200 = send traffic, 503 + `reasons[]` = do not (readiness probe, LB health check) |
| `GET /api/cluster` | `api:read` | instances, leader, leases, placements, preflight, coordinator |
| `GET /api/cluster/preflight` | `api:read` | re-run the checks now |
| `POST /api/cluster/leases/{name}/release` | `admin:write` | hand a lease over (also clears a dead instance's stale row) |
| `GET /api/health` | optional | plugin health round (cached 10 s) + `ready`; **not** a load-balancer check |
| `POST /api/system/restart` | `admin:write` | cluster mode: staggered rolling restart of every instance, leader last, no Docker socket needed |

## Testing note

The core test suite assumes single mode. When the container you run it in is a cluster instance,
override the environment: `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local`.
