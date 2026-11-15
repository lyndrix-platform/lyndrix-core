# Configuration reference

All names follow the existing prefixes. Helm values and IaC variables are only transport for
exactly these values — Compose and Kubernetes operators share one vocabulary.

| Variable | Default | Tier | Meaning |
|---|---|---|---|
| `LYNDRIX_CLUSTER_MODE` | `single` | 1 | `single` \| `cluster`. `cluster` turns on the `db` coordinator, preflight, drain default 10 s |
| `LYNDRIX_INSTANCE_ID` | `<hostname>-<4 hex>` | 1 | id for diagnostics and lease holder names; a new id after a restart is harmless, a fixed one is fine (each start is a new *incarnation*). It must be **unique among running processes**: when a second process registers under the same id, the older one stands back (`/api/ready` 503 `instance_id_conflict`, leases dropped) until the other is gone |
| `LYNDRIX_INSTANCE_ROLES` | single: `api,leader,runner` · cluster: `api,leader` | 1 | roles of this instance (see [Architecture](architecture.md#instances-and-roles)) |
| `LYNDRIX_COORDINATOR` | single: `local` · cluster: `db` | 1 | coordinator driver; `cluster` + `local` is rejected at startup |
| `LYNDRIX_LEASE_TTL_SECONDS` | `30` | 1 | a lease not renewed within the TTL is up for grabs — this is the failover time for a dead instance |
| `LYNDRIX_LEASE_HEARTBEAT_SECONDS` | `10` | 1 | heartbeat / renewal interval; must fit at least twice into the TTL |
| `LYNDRIX_EVENT_POLL_MS` | `500` | 1 | driver `db`: poll interval of `cluster_events` (≥ 100) |
| `LYNDRIX_TRUSTED_PROXIES` | `127.0.0.1` | 0 | exported as uvicorn `FORWARDED_ALLOW_IPS` by `docker/entrypoint.sh`; set to the load balancer address(es) or CIDR. `*` trusts `X-Forwarded-For` from every sender — only safe while nothing but the balancer can reach the core port (preflight warns; see [Security → Password logins](../security.md#password-logins)) |
| `LYNDRIX_DRAIN_SECONDS` | single `0` · cluster `10` | 0/1 | SIGTERM → at once: `/api/ready` 503, `draining` published to the peers, leases handed over (`lost` callbacks first) → wait → close SSE → tombstone → exit |
| `LYNDRIX_RESTART_INSTANCE_TIMEOUT_SECONDS` | `300` | 1 | rolling restart: how long the leader waits for one restarted instance to be ready again before it aborts the run |
| `LYNDRIX_STORAGE_SHARED_ROLES` | `api` | 1 | roles whose instances must see one shared `STORAGE_DIR` (comma list; they serve user files). The preflight `storage` check compares only those; empty = nobody has to share. An instance with role `api` on another host with its own `STORAGE_DIR` (e.g. a Compose core next to a Kubernetes cluster) is a permanent `storage: warn` — either keep it out of the user-facing load balancer and name only the roles of the shared instances here (e.g. `leader` when only those carry it), or mount the shared volume there |
| `LYNDRIX_PREFLIGHT_INTERVAL_SECONDS` | `300` | 2 | cluster mode: how often the preflight re-checks the cluster contracts (minimum 30). A check that could not decide gets at most five quick re-runs (every two heartbeats, also after a recovered heartbeat), then this interval applies; an unchanged warning is logged once (the `PREFLIGHT: … warn` summary line still counts it every run) |
| `LYNDRIX_READY_PLUGIN_WAIT_SECONDS` | `300` | 1 | cluster mode: after boot `/api/ready` stays 503 (`plugins:converging`) until the plugins recorded in `plugin_states` are installed and activated on this instance — at most this long, then it turns ready anyway (preflight `plugins: warn`). `0` = do not wait |
| `LYNDRIX_READY_REQUIRES_VAULT` | `false` | 1 | whether a Vault outage **after the boot** fails `/api/ready`. `false`: the outage is reported (`/api/ready` → `degraded: ["vault"]`, `/api/health` → `status: degraded`, preflight `vault: warn`, log) and the instance stays in rotation — see [Readiness](#readiness). `true`: `/api/ready` answers 503 `vault` until Vault is back; only sensible when Vault itself is highly available. During boot Vault gates readiness either way |
| `LYNDRIX_RESTART_GRACE_SECONDS` | `90` | 1 | how long an instance that stopped gracefully counts as `restarting` (not `dead`) for its peers: `ctx.has_live_role()` stays true, pinned plugins answer `Retry-After: 5`. Should cover a container restart |
| `LYNDRIX_PLUGIN_TEARDOWN_TIMEOUT` | `15` | 0/1 | seconds a plugin's `teardown()` may take on disable/unload/reload before the core stops waiting and continues |
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
| `LYNDRIX_PLUGIN_REPO_HOSTS` | `github.com` | 0 | comma-separated git hosts the plugin installer may talk to (install / upgrade / version list / custom repositories) |
| `LYNDRIX_PLUGIN_GITLAB_TOKEN_HOST` | — = the single non-GitHub host in `LYNDRIX_PLUGIN_REPO_HOSTS` | 2 | the one host the global GitLab token (`GITLAB_TOKEN` / Vault `gitlab_token`) is sent to — over https only, never across a redirect. With several GitLab hosts on the allowlist and this unset, the token is not used at all |
| `LYNDRIX_PLUGIN_PIP_INDEX_URL` | — = PyPI | 2 | package index for plugin requirement installs (internal mirror). pip runs `--isolated` with a minimal environment, so `PIP_INDEX_URL` / `pip.conf` of the container are ignored on purpose |
| `LYNDRIX_PLUGIN_PIP_CACHE_DIR` | — | 2 | optional shared wheel cache for the followers of a cluster; only on storage nobody but the cores can write ([plugins](building-blocks/plugins.md)) |
| `LYNDRIX_PLUGIN_MAX_ARCHIVE_MB` · `LYNDRIX_PLUGIN_MAX_EXTRACTED_MB` · `LYNDRIX_PLUGIN_MAX_ARCHIVE_FILES` | `200` · `1024` · `20000` | 2 | limits for a plugin archive: streamed download size, sum of the extracted files, number of entries — an install beyond them fails |
| `LYNDRIX_THEME_MAX_ZIP_MB` | `50` | 2 | upper bound for an uploaded theme ZIP (upload and extracted size); `tokens.json` / `components.json` are capped at 2 MB, images at 8 MB each |

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
| `plugins` | — | this instance has not finished installing/activating the cluster's plugins yet (not ready), or gave up waiting after `LYNDRIX_READY_PLUGIN_WAIT_SECONDS` (ready, plugins may be missing) |
| `roles` | a `pinned:<role>` plugin has no live instance with that role; no leader-capable instance | — |
| `ui_engine` | — | NiceGUI enabled in cluster mode |
| `trusted_proxies` | — | `X-Forwarded-For` arrives (or cluster mode) while `LYNDRIX_TRUSTED_PROXIES` is the default; or it is `*` (client addresses can be forged by anyone who reaches the core port directly) |
| `database` | — | `DB_HOST` is a Galera node (`wsrep_on`) and `DB_ALLOW_DIRECT_NODE` is not set |
| `clock` | — | this instance's clock is more than 5 s off the database clock |
| `vault` | — | Vault is not available to this instance (unreachable or sealed after the boot); secrets and Vault-stored settings cannot be read or written until the watchdog sees it back |
| `vault_nodes` | — | a node in `VAULT_NODES` does not answer `/v1/sys/health` |
| `drain` | — | cluster mode with `LYNDRIX_DRAIN_SECONDS=0` |
| `storage` | — | cluster mode: an instance that serves user files (`LYNDRIX_STORAGE_SHARED_ROLES`) does not share `STORAGE_DIR` with this one (its marker is not readable here — named in the message), no second such instance is alive so it cannot be verified, or the mount does not respond (a storage job is stuck) |

## Readiness

`GET /api/ready` answers 503 with one or more `reasons`:

| Reason | Meaning |
|---|---|
| `boot:<phase>` | boot has not finished (or failed) |
| `database` | not connected |
| `vault` | Vault has not been opened yet (boot), or — only with `LYNDRIX_READY_REQUIRES_VAULT=true` — it was lost after the boot |
| `draining` | SIGTERM received, the instance is leaving |
| `coordinator` | cluster mode: the coordinator has not started yet (it retries with back-off) |
| `plugins:converging` | cluster mode: boot finished, but the plugins of the cluster are still being installed/activated here (new pod with an empty plugin directory). Ends when they are, or after `LYNDRIX_READY_PLUGIN_WAIT_SECONDS`. An install that failed because the network or the package index did not answer counts as still converging while its quick retries (30/60/120/300 s) run |
| `instance_id_conflict` | cluster mode: a process that started later registered under this `LYNDRIX_INSTANCE_ID`; this (older) process stands back until the other stops |
| `heartbeat` | cluster mode: no heartbeat succeeded for a full lease TTL — the peers already count this instance as dead (no leases, no shards), so it must not receive traffic either. Clears with the next successful beat |
| `preflight:storage_secret` | this instance's `STORAGE_SECRET` differs from the cluster's |

Coordinator health is read **live** (`coordinator`, `heartbeat`); the cached `coordinator` preflight
check is display only, so a single failed query during a preflight can no longer keep an instance
out of rotation for minutes. Of the preflight checks only `storage_secret` gates readiness. A
cluster-level failure such as a missing runner instance, or a `STORAGE_DIR` mount that hangs, is
shown on the Cluster page and in the logs but does not take instances out of rotation — the mount
is shared, so every instance would leave at once.

### Vault outage after the boot

All instances usually share **one** Vault. When it restarts, re-seals or is cut off, the seal
watchdog of every instance notices within two probes (30 s apart, the second one after 10 s) and
marks Vault as lost. By default that does **not** fail readiness — taking every instance out of
rotation at once would turn a Vault restart into an outage of the whole platform:

- `/api/ready` stays 200 and lists `"degraded": ["vault"]`; `/api/health` reports `status: degraded`
  (never better) and, for authenticated callers, `degraded: ["vault"]`; the preflight check `vault`
  warns; the log has one `WATCHDOG:` line for the loss and one for the recovery.
- Keeps working: logins (with the provider configuration already loaded), sessions and API keys,
  SSE, notifications, everything read from the database, running plugins with the settings they
  already hold in memory.
- Does not work until Vault is back: reading or writing secrets (`ctx.get_secret` returns nothing,
  `ctx.set_secret` fails), saving core, auth or plugin settings (an error, nothing is written — a
  read-modify-write never writes over data it could not read), the Vault-stored system API key
  (requests using it get `401`; the `LYNDRIX_SYSTEM_API_KEY` environment variable is not affected),
  installs from private plugin repositories whose token lives in Vault.
- Recovery is automatic: while Vault is lost the watchdog probes every 10 s; once Vault answers
  unsealed it restores the token and re-emits `vault:opened` / `vault:ready_for_data`. A Vault that
  comes back **sealed** is unsealed automatically when `LYNDRIX_MASTER_KEY` is set, otherwise it
  waits for the operator.

`LYNDRIX_READY_REQUIRES_VAULT=true` restores the strict behaviour (503 `vault` while it is away) —
for deployments where Vault is highly available and an instance that lost it has a local problem.
A freshly started instance never becomes ready without Vault, with either value.

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/live` | none | process is up (liveness probe) |
| `GET /api/ready` | none | 200 = send traffic, 503 + `reasons[]` = do not (readiness probe, LB health check) |
| `GET /api/cluster` | `api:read` | instances, leader, leases, placements, preflight, coordinator |
| `GET /api/cluster/preflight` | `api:read` | re-run the checks now |
| `POST /api/cluster/leases/{name}/release` | `admin:write` | hand a lease over: the live holder stops its work and releases (asked over the bus when it is another instance); a dead instance's stale row is freed directly, as is any lease with `?force=true` |
| `GET /api/health` | optional | plugin health round (cached 10 s) + `ready`; **not** a load-balancer check |
| `POST /api/system/restart` | `admin:write` | cluster mode: sequential rolling restart driven by the leader — one instance at a time, each must be ready again before the next, leader last; runners only with `?include_runners=true`; 409 when no leader can coordinate it |

## Testing note

The core test suite assumes single mode. When the container you run it in is a cluster instance,
override the environment: `-e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local`.
