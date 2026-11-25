# Implementation concept: HA modular — Docker **and** Kubernetes, building blocks instead of a blueprint

**Status:** 🚧 M0 + M1 implemented (Core 1.4.0, plugin API 1.7.0, branch `feature/ha-cluster`); M2–M6 open
**Created:** 2026-10-14, continuation of [`high-availability.md`](./high-availability.md) (analysis + target architecture)

> **Implementation status 2026-10-14.** Delivered (core `feature/ha-cluster`, UI `feature/ha-cluster`,
> monitoring `feature/ha-lease` v1.4.0, orchestrator `feature/ha-placement` v1.3.0):
> `cluster` component with drivers `local`/`db` (tables `cluster_*`, READ COMMITTED + retry
> 1020/1205/1213, DB clock), LeaseKeeper (heartbeat, fencing after TTL, release broadcast →
> takeover ≈ 1 s on orderly stop, ≤ TTL on kill), bus bridge with allowlist + `emit(cluster=True)`
> + `_origin`, SSE relay and `event: reconnect` on drain, `/api/live` + `/api/ready` +
> `/api/cluster{,/preflight,/leases/{name}/release}`, preflight (13 checks; only `coordinator`
> and `storage_secret` remove the instance from rotation), drain on SIGTERM, staggered cluster restart
> without Docker socket, bootstrap DDL/seeds under `GET_LOCK('lyndrix_bootstrap')`,
> DB reconnect endlessly without re-bootstrap (`db:reconnected`, no double `load_all`), OIDC state/
> SSO codes/login limiter in the cluster KV, OIDC callback without NiceGUI, settings propagation
> (`settings:changed`), plugin API `placement` + `ctx.on_lease/lease/is_leader/instance_id/cluster_size/roles`,
> `pinned` = 503 `remote` (decision §8.5: simple variant), plugin artifact reconcile from
> `plugin_states` (`plugin:desired_changed` + 5-min pass), boot screen lines live, cluster page
> in the shell (Settings → Cluster), English documentation tree `docs/ha/` (decision §8.2),
> two-instance test bench `docker/docker-compose.ha.dev.yml` (Traefik `:8090`, `/api/ready` check).
> Verified on the test bench: leader/prober failover, fan-out, LB round robin, drain.
> **Not delivered:** `lyndrix-deploy` (§5), storage driver (§3.2), migrate CLI (§3.3),
> `VAULT_NODES` unseal/AppRole, Redis driver (§8.4: only when needed), Helm chart, Galera/Raft
> reference stacks, remaining plugins from M4 (docker-manager, bingo, discord, itsm attachments, ssot-manager).
> Decisions §8.1 (repo), §8.3 (LB options in docs: all seven included), §8.6 (cluster
> page in the core: yes), §8.7 (tier 1 officially: marked as such in `docs/ha/tiers.md`) — please confirm.
**Affected repos:** `lyndrix-core` (cluster/storage component, preflight, drain, documentation tree), `lyndrix-ui` (cluster page), all plugins (placement/leases), **new** `lyndrix-deploy` (Compose HA bundles, LB reference configs, Helm chart), `iac-environment` (own SSoTs)

> Operator requirements: HA must be buildable **both directly on Docker and on k3s**; for Docker a
> **separate load balancer** is required, which is documented in the application docs as an
> option; the docs get a **completely new HA tree**, because not everyone wants HA — whoever wants it should be able to decide
> **as modularly and openly as possible**.
> Convention: prose German, code/identifiers/APIs English.

---

## 0. Guiding principles

1. **The core knows neither Docker nor Kubernetes.** It only knows environment variables and
   *contracts* (what a load balancer, a database, a Vault, a storage must provide).
   Deployment profiles are artifacts and documentation *outside* the code. This way every core change
   automatically works on both platforms.
2. **Opt-in, never opt-out.** `LYNDRIX_CLUSTER_MODE=single` is the default and behaves exactly as
   today. The coordinator then runs with the driver `local` (leader = always me, every lease is granted,
   events stay local). Plugins write **one** code path that runs in both modes.
3. **Building blocks instead of a blueprint.** Each layer (LB, coordination, DB, secrets, storage, plugins,
   frontend) has a *contract* and *N options* with a comparison. The core **validates** the
   contract (preflight) instead of prescribing one option.
4. **Stages you may stop at** (§1): robust single instance → app HA →
   data HA → DR. Each stage is documented on its own, honestly with its remaining SPOFs.
5. **Same operator experience on both platforms:** the same env names (Helm values
   map 1:1), the same cluster page in the UI, the same preflight check, the same drills.

---

## 1. Tier model

| Tier | Name | What runs N-fold | Remaining SPOFs | Minimum required |
|---|---|---|---|---|
| **0** | Robust single instance | nothing | everything | backups, `/api/ready`, healthcheck, restart policy, drain |
| **1** | App HA | core API, frontend | DB, Vault, storage | LB, coordination (`db`), same `STORAGE_SECRET`, identical plugin artifacts, worker role for orchestrator |
| **2** | Data HA | + DB (Galera, 1 writer), Vault (Raft) | storage (gracefully degrading), site | MaxScale/proxy, `VAULT_NODES`, shared/object storage |
| **3** | DR | backup target at the second site, restore runbook | — | off-site dumps/snapshots, documented recovery |

Tier 1 is deliberately standalone: many want rolling updates without downtime and survival of a dead
core container without having to operate Galera right away. The docs clearly state what tier 1 **does not** cover.

---

## 2. Configuration surface — the one truth for Compose, Helm and SSoT

All names follow the existing prefixes (`LYNDRIX_*`, `DB_*`, `VAULT_*`). Helm values
and SSoT variables are merely transport paths for exactly these values.

| Variable | Default | Tier | Meaning |
|---|---|---|---|
| `LYNDRIX_CLUSTER_MODE` | `single` | 1 | `single` \| `cluster`. `cluster` activates coordinator, mandatory preflight, React-only, drain |
| `LYNDRIX_INSTANCE_ID` | auto (`<hostname>-<rand>`) | 1 | stable identifier only needed for diagnostics; a new ID after restart is harmless (leases expire) |
| `LYNDRIX_INSTANCE_ROLES` | single: `api,leader,runner` · cluster: `api,leader` | 1 | `api` (routes), `leader` (lease-capable for core one-time tasks + `singleton` plugins), `runner` (for `pinned:runner` plugins; needs Docker socket) |
| `LYNDRIX_COORDINATOR` | single: `local` · cluster: `db` | 1 | `local` \| `db` \| `redis` |
| `LYNDRIX_REDIS_URL` | — | opt. | only driver `redis` |
| `LYNDRIX_LEASE_TTL_SECONDS` / `LYNDRIX_LEASE_HEARTBEAT_SECONDS` | `30` / `10` | 1 | lease semantics |
| `LYNDRIX_EVENT_POLL_MS` | `500` | 1 | driver `db`: poll interval `cluster_events` |
| `LYNDRIX_MIGRATE_MODE` | `auto` | 0 | `auto` (boot under `GET_LOCK`), `job` (instances perform **no** DDL, only check the schema version → Compose init service / k8s job runs `lyndrix migrate`), `off` |
| `LYNDRIX_PLUGIN_ARTIFACTS` | `local` | 1 | `local` (as today), `reconcile` (every instance pulls `plugin_states.desired_version` itself), `image` (baked into the image, installs only as DB request + hint "build new image") |
| `LYNDRIX_STORAGE_DRIVER` | `local` | 1/2 | `local` \| `fs` (shared mount, checked for writability + commonality) \| `s3` |
| `LYNDRIX_STORAGE_S3_ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY/REGION` | — | opt. | driver `s3` (MinIO, Garage, SeaweedFS, AWS) |
| `LYNDRIX_TRUSTED_PROXIES` | `127.0.0.1` | 0 | → uvicorn `--forwarded-allow-ips`; mandatory behind every LB (rate limiter, audit IP) |
| `LYNDRIX_DRAIN_SECONDS` | single `0` · cluster `10` | 0/1 | SIGTERM → `/api/ready` 503 → wait → close SSE → release leases → exit |
| `LYNDRIX_UI_ENGINE` | `react` | 1 | enforced `react` in `cluster`; `LYNDRIX_ALLOW_LEGACY_UI_IN_CLUSTER=1` only with sticky LB + shared `/app/.nicegui` (documented as "not supported, but possible") |
| `DB_HOST` | — | 0 | **writer endpoint** (MaxScale/ProxySQL/service). Never a Galera node directly with N instances |
| `DB_POOL_RECYCLE_SECONDS` / `DB_RETRY_ATTEMPTS` | `300` / `3` | 1 | failover tolerance (1213/1205/2006/2013) |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `60` | 0 | present (API 1.5) |
| `VAULT_URL` | — | 0 | LB or active endpoint |
| `VAULT_NODES` | — | 2 | list of all nodes for health poll + unseal (`https://v1:8200,https://v2:8200,…`) |
| `VAULT_UNSEAL_MODE` | `lyndrix` | 2 | `lyndrix` (leader unseals all `VAULT_NODES` with the keyfile), `external` (transit/KMS/auto-unseal on Vault side; core only polls), `manual` |
| `VAULT_AUTH` | `root-file` | opt. | `root-file` (today) \| `approle` (`VAULT_ROLE_ID`, `VAULT_SECRET_ID`, renewal loop) |
| `LYNDRIX_MASTER_KEY` + `SECURITY_DIR/vault_keys.enc` | — | 0 | unchanged; in HA as secret/shared secret to every instance |
| `STORAGE_SECRET` | — | 0 | unchanged; must be identical on all instances (preflight checks hash in `cluster_instances`) |

**Preflight** (`GET /api/cluster/preflight`, also `lyndrix doctor`) checks in `cluster` mode and
refuses start on hard violations (with plain-text message naming the violated contract):
`STORAGE_SECRET` identical; `DB_HOST` is not a Galera node directly (`@@wsrep_on` + `@@wsrep_node_name`
vs. proxy detection) or explicitly `DB_ALLOW_DIRECT_NODE=1`; schema version matches; `VAULT_NODES`
all reachable; storage driver is not `local` if plugins store attachments; at least one
instance with role `runner` if a `pinned:runner` plugin is active; `LYNDRIX_TRUSTED_PROXIES`
set if `X-Forwarded-For` arrives; clock drift between instances < 5 s (leases!).

---

## 3. Core building blocks (code)

### 3.1 Component `core/components/cluster/`

```
cluster/
├── logic/
│   ├── coordinator.py      # Protocol + factory (LYNDRIX_COORDINATOR)
│   ├── drivers/local.py    # single: leases always granted, publish = no-op
│   ├── drivers/db.py       # tables cluster_instances/leases/kv/events, poll loop
│   ├── drivers/redis.py    # SET NX PX, Pub/Sub, INCR/EXPIRE (later)
│   ├── leases.py           # LeaseKeeper: heartbeat, callbacks acquired/lost, jitter
│   ├── bridge.py           # bus bridge: allowlist topics ↔ publish/subscribe, _origin
│   ├── preflight.py        # contract checks (§2)
│   └── drain.py            # SIGTERM handling, ready=False, release leases
├── api/cluster_api.py      # GET /api/cluster, /preflight, /leases; POST /leases/{name}/release (admin)
└── ui/react/               # cluster page (in lyndrix-ui: instances, leader, leases, placements, preflight)
```

```python
class Coordinator(Protocol):
    instance_id: str
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def try_acquire(self, name: str, ttl: int) -> bool: ...
    async def release(self, name: str) -> None: ...
    async def holder(self, name: str) -> str | None: ...
    async def kv_set(self, key: str, value: Any, ttl: int) -> None: ...
    async def kv_get(self, key: str) -> Any | None: ...
    async def kv_incr(self, key: str, ttl: int) -> int: ...          # rate limiter
    async def publish(self, topic: str, payload: dict) -> None: ...
    def subscribe(self, callback: Callable[[str, dict], Awaitable[None]]) -> None: ...
    async def instances(self) -> list[InstanceInfo]: ...
```

Consumers in the core: leader lease `core:leader` (seeds in `auto` mode, collection sync,
update checker, messaging outbox, vault unseal poll, `singleton` plugin leases), OIDC state +
SSO codes + limiter → `kv_*`, bridge for the allowlist topics from the analysis (§9.4 there),
SSE relay, `/api/health` → `/api/live` + `/api/ready` + deep cached.

### 3.2 Component `core/components/storage/`

`ObjectStore` protocol (`put/get/delete/exists/url/list`) with drivers `local`, `fs`, `s3`;
exposed as `ctx.storage` (namespace per plugin: `plugins/<id>/…`). Migrates: theme uploads,
user backgrounds, ITSM attachments, plugin zip cache. The driver `fs` is technically `local` plus
a preflight "is the directory the same mount on all instances?" (marker file with
`instance_id`, read by the others).

### 3.3 Migration and bootstrap

- CLI `python -m app.cli migrate` (image entrypoint `entrypoint.sh migrate`): `create_all`, all
  `ALTER`s scattered today, seeds — **idempotent**, under `GET_LOCK('lyndrix_migrate', 120)`,
  writes `lyndrix_schema(component, version)`.
- Plugins replace DDL in `setup()`/`health()` with `ctx.migrate(fn)`: the core runs `fn` in the
  migration job or on the leader under lock; instances without a matching schema version wait
  (`starting`, not blocking → routes 503 with hint) instead of aging themselves.
- Compose: `init` service `lyndrix-migrate` with `depends_on: condition: service_completed_successfully`;
  k8s: `Job` as Helm hook `pre-install,pre-upgrade`.

### 3.4 Plugin artifacts

Driver `reconcile`: reconciler per instance compares `plugin_states.desired_version` with the
manifest version in `/app/plugins/<repo>/entrypoint.py`, on deviation loads (zip from release tag,
`vendor` via pip), reloads the module — triggered by cluster event `plugin:desired_changed` and every
`LYNDRIX_PLUGIN_RECONCILE_SECONDS` (300). Marketplace install in the cluster = only DB request +
event; the leader verifies beforehand (download + health in tempdir), so that not N instances
simultaneously pull a broken plugin. Driver `image`: reconciler only reports deviation
(`plugin:artifact_mismatch`), installs nothing.

### 3.5 Drain and rolling update

SIGTERM → `ready=False` (`/api/ready` 503, LB removes the instance after 1–2 checks) → wait
`LYNDRIX_DRAIN_SECONDS` → close SSE streams with `event: reconnect` → release leases
(leader change immediately instead of after TTL) → `stop()` of plugins → exit. uvicorn
`--timeout-graceful-shutdown` remains the emergency brake behind it. k8s: `terminationGracePeriodSeconds`
≥ drain + 15.

### 3.6 Plugin API (core API 1.6)

```python
manifest = ModuleManifest(..., placement="any")   # any | singleton | pinned:<role>

def setup(ctx):
    ctx.register_routes(router)                                  # every api instance
    ctx.on_lease("monitoring:prober", acquired=svc.start, lost=svc.stop)
    ctx.emit("monitoring:monitors_changed", {...}, cluster=True)
    ctx.migrate(svc.ensure_schema)                                # instead of DDL in setup()
    store = ctx.storage                                          # ObjectStore, namespaced

async with ctx.lease("itsm:connector_poll") as held:
    if held: ...
ctx.is_leader, ctx.instance_id, ctx.cluster_size, ctx.roles
```

Compatibility as with `set_status`: `getattr(ctx, "on_lease", None)`; on old cores the
plugin falls back to "always active" (= today's behavior).

### 3.7 UI (`lyndrix-ui`)

Page **Settings → Cluster** (visible only in `cluster` mode): instances with roles, version,
heartbeat, leader marker; leases with holder and remaining time; plugin placements and where they run;
preflight result with plain text; button "Release lease" (admin). Runtime status pills show
`instance_id` when instances diverge.

---

## 4. The separate load balancer for Docker

### 4.1 LB contract (platform-neutral, documented as such)

1. Health check **`GET /api/ready`** (200 = traffic, anything else = out), interval ≤ 5 s,
   `fall 2 / rise 2`. Never `/api/health` as LB check (expensive, checks plugins).
2. **SSE**: no response buffering, read/tunnel timeout ≥ 1 h for `/api/events` and
   `/api/plugins/*/stream/*`.
3. **Forwarded headers** (`X-Forwarded-For/Proto/Host`) set; core side `LYNDRIX_TRUSTED_PROXIES`.
4. **One public origin** for UI + API (TLS termination at the LB or passthrough).
5. **WebSocket** upgrade only if the legacy UI is allowed; then **sticky cookie** mandatory.
6. Optional: pass through `Retry-After`/503 from the runtime gate (do not count as "backend dead",
   i.e. health check separate from request errors).
7. The LB **itself** must not be a SPOF → VIP (keepalived/CARP) or two LBs + DNS failover.

### 4.2 Options (documentation comparison table)

| Option | Active health checks | TLS | SSE | Sticky | LB HA | When |
|---|---|---|---|---|---|---|
| **Traefik** (file provider, static servers) | yes (`healthCheck.path`) | ACME | yes (`responseForwarding.flushInterval`) | cookie | with keepalived | standard in the fleet; do **not** use container discovery (backends are on other hosts) |
| **HAProxy** | yes (`option httpchk`) | yes | `timeout tunnel 1h` | cookie/source | keepalived or **pfSense package + CARP** | most precise health semantics; appliance variant without own host |
| **Caddy** | yes (`health_uri`) | automatic | `flush_interval -1` | cookie | keepalived | smallest configuration, for beginners |
| **nginx OSS** | **passive only** (`max_fails`) | yes | `proxy_buffering off` | `ip_hash`/cookie | keepalived | if already present; active checks only in Plus |
| **Docker Swarm** routing mesh | container healthcheck | — | yes | no | built-in | if someone uses Swarm; no sticky, no `/api/ready` → `HEALTHCHECK` in image needed |
| **DNS round robin** | no | — | — | no | — | **not recommended**, transitional only (browser failover slow) |
| Hardware/cloud LB | yes | yes | mostly | yes | yes | if present; check contract §4.1 |

### 4.3 Where the LB runs

- **Dedicated pair** (cleanest separation, two small LXCs) with keepalived VIP.
- **Colocated** on every app host (3 hosts → 3× LB + core) with a VIP that moves; saves
  hosts, but couples LB and app failure.
- **Appliance**: pfSense HAProxy package + CARP VIP — fits the existing pfSense, no container.
- **k3s**: not needed — ingress + MetalLB/kube-vip take over the contract; the docs show the
  annotations (Traefik `traefik-external`, health via `readinessProbe`).

### 4.4 Shipped reference configurations (`lyndrix-deploy/docker/ha/lb/`)

`traefik/{traefik.yml,dynamic.yml}`, `haproxy/haproxy.cfg`, `caddy/Caddyfile`,
`nginx/lyndrix-lb.conf`, `keepalived/{keepalived.conf,check_lb.sh}`, `pfsense/README.md`
(click instructions + export). Each configuration implements §4.1 completely and can be checked with a
smoke script (`scripts/lb-contract-test.sh <url>`: ready check, SSE stream open for 70 s,
forwarded headers visible in `/api/system/info`).

---

## 5. Deployment profiles as artifacts: new repo `lyndrix-deploy`

Rationale for a separate repo: its own release cadence (chart version ↔ core image version),
one place for everything operator-relevant, no mixing with the dev compose in the core. Alternative
(in the core under `deploy/`) is possible, but loses the clean versioning of the chart.

```
lyndrix-deploy/
├── docker/
│   ├── single/                 docker-compose.yml  (tier 0: today's production setup + healthcheck + backups)
│   └── ha/
│       ├── core/               docker-compose.yml  (one api instance; DB/Vault/storage external via .env)
│       ├── runner/             docker-compose.yml  (role runner, docker.sock, HOST_* paths)
│       ├── lb/                 traefik | haproxy | caddy | nginx | keepalived | pfsense  (§4.4)
│       ├── db/                 galera-node/ (per host) + maxscale/ + bootstrap.sh + backup.sh
│       ├── vault/              raft-node/ (vault **or** openbao, same files) + join.sh
│       ├── storage/            minio/ (optional)
│       ├── migrate/            init service snippet
│       └── .env.ha.example
├── kubernetes/
│   ├── helm/lyndrix/           chart (values: core.replicas, core.roles, runner.enabled,
│   │                            database.mode external|operator|bundled, vault.mode external|raft|bundled,
│   │                            storage.mode, coordinator, ingress.className, migrate.job)
│   ├── kustomize/              base + overlays (k3s-traefik, ingress-nginx)
│   └── examples/               k3s-full-ha.values.yaml, external-db-vault.values.yaml, tier1.values.yaml
├── scripts/
│   ├── ha-preflight.sh         curl /api/cluster/preflight on every instance
│   ├── ha-rolling-update.sh    order: migrate → runner → api instances one by one, waits for /api/ready
│   ├── lb-contract-test.sh
│   └── failover-drill.sh       stop instance/DB node/Vault node, output measurement points
└── README.md → refers to the HA tree of the core docs
```

**Mapping Helm ↔ env** (excerpt): `core.roles` → `LYNDRIX_INSTANCE_ROLES`; `coordinator` →
`LYNDRIX_COORDINATOR`; `database.external.host` → `DB_HOST`; `vault.nodes` → `VAULT_NODES`;
`storage.s3.*` → `LYNDRIX_STORAGE_S3_*`; `migrate.job=true` → `LYNDRIX_MIGRATE_MODE=job`.
Every Helm option is listed in the docs with its env variable, so that Compose and k8s readers
share the same vocabulary.

**Marvin's fleet (iac-environment):** from the same reference files, SSoTs emerge:
`aac-lyndrix-core` (role `api`, one entry per host), `aac-lyndrix-runner`, `aac-lyndrix-lb`,
`aac-lyndrix-db`, `aac-lyndrix-vault`; the today's `dependencies:` DB/Vault in the core SSoT
are dropped. The engine property "same SSoT on several hosts = independent stacks" is exactly
what Galera/Raft nodes need.

---

## 6. The new documentation tree (`lyndrix-core/docs/ha/`)

**Language:** the MkDocs site (`docs/deployment.md` "Installation and Deployment") is English →
the HA tree will be **English**, so that the site stays consistent; `features/` concepts remain
German. (Decision §8.)

**Principle:** `deployment.md` remains the path for "I just want Lyndrix". The HA tree is its own
top-level entry, begins with a **decision guide** and ends with drills — whoever does not want HA
never has to open it.

```yaml
# mkdocs.yml (nav addition)
  - High Availability:
      - Overview & decision guide: ha/index.md          # questions: hosts? k8s? Vault/DB present? orchestrator? → tier + profile
      - Tiers: ha/tiers.md                              # tiers 0–3, what each stage does (not) cover
      - Architecture & contracts: ha/architecture.md    # layer picture, leader/leases/placement, contracts
      - Configuration reference: ha/configuration.md    # table §2, preflight rules
      - Building blocks:
          - Load balancer: ha/building-blocks/load-balancer.md
          - Coordination: ha/building-blocks/coordination.md       # local | db | redis
          - Database: ha/building-blocks/database.md               # single | galera+maxscale | galera+proxysql | primary/replica | operator | managed
          - Secrets (Vault / OpenBao): ha/building-blocks/secrets.md   # single | raft | external; unseal modes
          - Shared storage: ha/building-blocks/storage.md          # local | fs (NFS/CIFS/RWX) | s3
          - Plugin artifacts & placement: ha/building-blocks/plugins.md
          - Frontend: ha/building-blocks/frontend.md
      - Docker:
          - Overview & host layouts: ha/docker/index.md           # 3 hosts; 2 + arbiter; dedicated LB pair
          - The separate load balancer: ha/docker/load-balancer.md  # §4 with all reference configs
          - Galera + MaxScale: ha/docker/galera-maxscale.md
          - Vault / OpenBao Raft: ha/docker/vault-raft.md
          - Compose reference: ha/docker/compose-reference.md
          - Rolling updates: ha/docker/rolling-updates.md
      - Kubernetes:
          - Overview: ha/kubernetes/index.md
          - Helm chart: ha/kubernetes/helm.md
          - Database options: ha/kubernetes/database.md           # mariadb-operator, Bitnami, external
          - Vault options: ha/kubernetes/vault.md                 # Helm Raft, external, unseal
          - Ingress & load balancing: ha/kubernetes/ingress.md
          - Rolling updates: ha/kubernetes/rolling-updates.md
      - Operations:
          - Cluster status & preflight: ha/operations/cluster-status.md
          - Backup & restore: ha/operations/backup-restore.md
          - Failover drills: ha/operations/failover-drills.md
          - Troubleshooting: ha/operations/troubleshooting.md
      - For plugin developers: ha/plugin-development.md           # placement, on_lease, cluster events, DDL rules, PK requirement
```

**Page template for each building block** (same grid, so that comparison is easy):
1. *Contract* — what Lyndrix requires from this layer (short, verifiable).
2. *Options* — table: option · HA of the layer itself · effort · special characteristics · when to choose.
3. *Lyndrix settings* — the affected variables from §2.
4. *Reference setup* — reference to `lyndrix-deploy/…` + minimal example.
5. *Verify* — concrete commands (preflight line, drill).
6. *Known limits*.

The decision guide (`ha/index.md`) is a short question tree: *How many hosts? (1 → tier 0;
2 → tier 1 or 2 + arbiter; ≥ 3 → everything) · Kubernetes present? (→ Kubernetes branch) · Existing
Vault/OpenBao? (→ `vault.mode external`) · Existing DB HA? (→ `database.mode external`, check
contract) · IaC orchestrator in use? (→ plan runner role) · Legacy UI needed? (→ sticky LB,
not supported)* — each answer links the matching page.

---

## 7. Implementation sequence with milestones (both platforms testable early)

| M | Result | Content | Proof |
|---|---|---|---|
| **M0** | Tier 0 is real | `/api/live` + `/api/ready`, drain, `LYNDRIX_TRUSTED_PROXIES`, `migrate` CLI + `GET_LOCK`, boot races idempotent, Vault health poll, notifications → DB, logs stdout, backup scripts in `lyndrix-deploy/docker/single` | DB stop/start without re-bootstrap; restore from dump on `docker-test` |
| **M1** | Core `cluster` mode (API 1.6) | `cluster` component with `local`+`db`, bridge, SSE relay, leader tasks, kv for OIDC/limiter, `reconcile` artifacts, preflight, cluster API; **dev compose with 2 instances behind `docker/traefik.dev.yml`** as mandatory test bench | install on A visible on B; leader kill → takeover ≤ 30 s; OIDC with instance change |
| **M2** | Docker tier 1 end-to-end | `lyndrix-deploy/docker/ha/{core,runner,lb}` + all LB reference configs + `lb-contract-test.sh`; docs `ha/docker/*` + `ha/building-blocks/load-balancer.md` | 2 hosts + LB pair with keepalived: rolling update without 5xx; kill instance invisibly |
| **M3** | k3s tier 1 | Helm chart (`database.mode external`, `vault.mode external`, migrate job, probes, PDB, `terminationGracePeriodSeconds`), Kustomize overlay k3s-Traefik; docs `ha/kubernetes/*` | chart on the existing k3s with external test DB/Vault; `kubectl rollout restart` without 5xx |
| **M4** | Plugins cluster-capable | monitoring (lease prober, UNIQUE + atomic counters), orchestrator (`pinned:runner`, `owner_instance`, reservation, trigger events), ssot-manager (replace/pin), docker-manager/discord/bingo → DB, server-manager/registry/itsm `ctx.migrate` + retry, itsm attachments → `ctx.storage`, external-services sidebar from DB | heartbeat rate with 2 instances = with 1; orchestrator job survives API restart |
| **M5** | Tier 2 on both platforms | `docker/ha/{db,vault,storage}` (Galera+MaxScale, Raft, MinIO) + `VAULT_NODES` unseal + DB retry; Helm `database.mode operator` (mariadb-operator), `vault.mode raft`; docs `galera-maxscale`, `vault-raft`, k8s `database`/`vault` | kill writer → ≤ 1 retry; seal Vault node → leader unseals ≤ 20 s |
| **M6** | Docs tree complete + drills | decision guide, tiers, operations, plugin developer page, `failover-drill.sh`; SSoTs in the fleet from the reference files | stop hypervisor → `lyndrix.int` keeps responding |

M0–M1 are pure core work and immediately useful for Marvin's production instance. M2 and M3
both depend only on M1 and can run in parallel — this ensures that no core detail
accidentally becomes Docker- or k8s-specific. M4 is independent of deployment and can proceed
plugin by plugin after M1. M5 is infrastructure and needs only `VAULT_NODES`
and the DB retry from the core (take both along in M1).

---

## 8. Decisions for Marvin

1. **Own repo `lyndrix-deploy`** (recommended) or `lyndrix-core/deploy/`?
2. **Language of the HA docs tree:** English like the existing MkDocs site (recommended) or German
   per workspace convention?
3. **LB options in the docs:** Traefik + HAProxy + keepalived are mandatory; include **Caddy** (beginners)
   and **pfSense HAProxy + CARP** (appliance)? Mention Docker Swarm only or as a profile?
4. **Driver `redis`** in M1 (small extra effort, same interface) or only when needed?
5. **`pinned` semantics** for api instances: 503 "remote" (simple, M1) or transparent proxy to the
   worker (seamless, later)?
6. **Cluster page** in core/shell (recommended: platform state) or as a plugin?
7. **Tier 1 as official support state** (N core + LB, but single DB/Vault) — declare it so: yes?

---

## 9. Relation to the analysis

All findings with `file:line`, the target architecture and the phase logic are in
[`high-availability.md`](./high-availability.md); this document translates them into configuration,
code structure, artifacts, documentation tree and milestones. The analysis phases 0–4 correspond here to
M0 → M1 → M2/M3 → M4 → M5/M6.
