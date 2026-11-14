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
| `LYNDRIX_LOG_DB_ENABLED` | `true` | 1 | ship this instance's log lines to the shared `cluster_logs` table ([central log](building-blocks/logs.md)) |
| `LYNDRIX_LOG_DB_LEVEL` · `LYNDRIX_LOG_DB_EXCLUDE` | `INFO` · `uvicorn.access` | 2 | minimum level / excluded logger names (trailing `*` = prefix) |
| `LYNDRIX_LOG_DB_RETENTION_HOURS` · `LYNDRIX_LOG_DB_MAX_PER_INSTANCE` | `24` · `5000` | 2 | the leader prunes by age and per-instance cap |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `60` | 0 | per-connection statement cap (API 1.5) |
| `DB_SOCKET_TIMEOUT_SECONDS` | `0` = auto (cap + 30 s, min 90) | 1 | pymysql `read_timeout`/`write_timeout` of every pooled connection (core 1.11.1): a silently dead DB peer (VIP failover, half-open TCP) fails after this instead of the kernel's ~16 min; a value not above the statement cap is raised |
| `VAULT_URL` | `http://vault:8200` | 0 | load-balanced or active Vault endpoint |
| `VAULT_NODES` | — | 2 | comma-separated list of every Vault node; today only health-checked by the preflight |
| `LYNDRIX_MASTER_KEY` + `SECURITY_DIR/vault_keys.enc` | — | 0 | unchanged; in HA the key file is a shared secret on every instance |
| `STORAGE_SECRET` | — | 0 | unchanged; **must be identical on every instance** (preflight compares fingerprints; the instance(s) whose value differs from the majority stay out of rotation). Rotating it needs a restart of **all** instances with the new value — in a rolling update the first new instance is the odd one and stays unready |

Planned but not shipped (documented so the names are reserved): `LYNDRIX_MIGRATE_MODE`,
`LYNDRIX_PLUGIN_ARTIFACTS`, `LYNDRIX_STORAGE_DRIVER` / `LYNDRIX_STORAGE_S3_*`, `VAULT_UNSEAL_MODE`,
`VAULT_AUTH`, `LYNDRIX_REDIS_URL`.

## Preflight rules

The preflight runs after boot, whenever the set of live instances changes, every five minutes,
and on demand (`GET /api/cluster/preflight`, Settings → Cluster → *Re-run checks*). It runs in its
own task — never inside the heartbeat — and every check that does I/O is bounded to 8 s: a check
that times out or hits a transient error reports `warn` and is repeated two heartbeats later (at
most five times in a row, then at the normal interval). Each check is `ok`, `warn`, `fail` or
`skip`:

| Check | fail | warn |
|---|---|---|
| `coordinator` | cluster mode and the coordinator never started | the instance list could not be refreshed (the last known view is used), or no heartbeat succeeded within the lease TTL |
| `redundancy` | — | only one live instance |
| `storage_secret` | **this** instance's `STORAGE_SECRET` fingerprint differs from the one most live instances share (tie: the longest-running instance's value wins) | another instance's fingerprint differs (named in the message; that instance fails its own check) |
| `versions` | — | mixed core versions (expected during a rolling update) |
| `plugin_artifacts` | — | the loaded plugin set differs between instances |
| `roles` | a `pinned:<role>` plugin has no live instance with that role; no leader-capable instance | — |
| `ui_engine` | — | NiceGUI enabled in cluster mode |
| `trusted_proxies` | — | `X-Forwarded-For` arrives (or cluster mode) while `LYNDRIX_TRUSTED_PROXIES` is the default |
| `database` | — | `DB_HOST` is a Galera node (`wsrep_on`) and `DB_ALLOW_DIRECT_NODE` is not set |
| `clock` | — | this instance's clock is more than 5 s off the database clock |
| `vault_nodes` | — | a node in `VAULT_NODES` does not answer `/v1/sys/health` |
| `drain` | — | cluster mode with `LYNDRIX_DRAIN_SECONDS=0` |
| `storage` | — | cluster mode: `STORAGE_DIR` is not one shared mount (peer marker files missing/stale), only one instance is alive so it cannot be verified, or the mount does not respond (a storage job is stuck) |

## Readiness

`GET /api/ready` answers 503 with one or more `reasons`:

| Reason | Meaning |
|---|---|
| `boot:<phase>` | boot has not finished (or failed) |
| `database` / `vault` | not connected |
| `draining` | SIGTERM received, the instance is leaving |
| `coordinator` | cluster mode: the coordinator has not started yet (it retries with back-off) |
| `heartbeat` | cluster mode: no heartbeat succeeded for a full lease TTL — the peers already count this instance as dead (no leases, no shards), so it must not receive traffic either. Clears with the next successful beat |
| `preflight:storage_secret` | this instance's `STORAGE_SECRET` differs from the cluster's |

Coordinator health is read **live** (`coordinator`, `heartbeat`); the cached `coordinator` preflight
check is display only, so a single failed query during a preflight can no longer keep an instance
out of rotation for minutes. Of the preflight checks only `storage_secret` gates readiness. A
cluster-level failure such as a missing runner instance, or a `STORAGE_DIR` mount that hangs, is
shown on the Cluster page and in the logs but does not take instances out of rotation — the mount
is shared, so every instance would leave at once.

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
