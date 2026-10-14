# High Availability — overview and decision guide

Lyndrix runs perfectly well as **one** core instance, and that stays the default. This section is
for operators who want more than one: rolling updates without downtime, surviving a dead
container or hypervisor, and later a database and secrets tier that survive a node loss.

HA in Lyndrix is **opt-in and modular**. The core does not know Docker or Kubernetes; it knows
environment variables and *contracts* (what a load balancer, a database, a Vault and a shared
storage must provide). You pick the building blocks that fit your environment — a Docker host
pair with a separate load balancer, or a Kubernetes cluster with its ingress — and the core
validates the contracts with a **preflight** instead of prescribing one setup.

## What ships today (core 1.4.0, plugin API 1.7)

| Piece | Status |
|---|---|
| `LYNDRIX_CLUSTER_MODE=cluster`, coordinator driver `db` (instances, leader lease, TTL key/value, event fan-out through the shared MariaDB) | shipped |
| `GET /api/live`, `GET /api/ready` (load-balancer health check), `GET /api/cluster`, preflight | shipped |
| Drain on SIGTERM (`LYNDRIX_DRAIN_SECONDS`), SSE reconnect hint, graceful lease hand-over | shipped |
| Bus bridge: core topics and `ctx.emit(..., cluster=True)` reach every instance; SSE relay | shipped |
| Plugin API: `placement`, `ctx.on_lease`, `ctx.lease`, `ctx.is_leader`, `ctx.instance_id` | shipped |
| Plugin artifacts: instances reconcile `plugin_states` (install/enable/disable from any instance) | shipped (`reconcile` behaviour; no separate driver flag yet) |
| OIDC state, SSO exchange codes and the login rate limiter shared across instances | shipped |
| Cluster page in the React shell (Settings → Cluster) | shipped |
| First cluster-aware plugins: State Monitoring (one prober via lease), IaC Orchestrator (`pinned:runner`) | shipped |
| Shared storage driver (`ctx.storage`, `LYNDRIX_STORAGE_DRIVER`) | **not yet** — use a shared mount for `/data/storage` |
| Migration job / `LYNDRIX_MIGRATE_MODE` | **not yet** — DDL runs under a cross-instance lock on boot |
| Leader-driven multi-node Vault unseal (`VAULT_UNSEAL_MODE`), AppRole auth | **not yet** — `VAULT_NODES` is only health-checked |
| Reference stacks for Galera + MaxScale, Vault/OpenBao Raft, Helm chart (`lyndrix-deploy`) | **not yet** — contracts and options are documented |

Every "not yet" is listed in the matching building-block page under *Known limits*.

## Decision guide

Answer these in order; each answer points at a page.

1. **How many hosts can run a core instance?**
   - 1 → stay on [Tier 0](tiers.md#tier-0-robust-single-instance): health checks, restart policy,
     backups. Nothing else in this section is required.
   - 2 or more → Tier 1 is within reach: [Architecture](architecture.md), then the Docker or
     Kubernetes branch.
2. **Do you already run Kubernetes?**
   - Yes → [Kubernetes](kubernetes/index.md). The ingress takes the load-balancer contract.
   - No → [Docker](docker/index.md). You need a **separate load balancer**:
     [the LB page](docker/load-balancer.md) compares Traefik, HAProxy, Caddy, nginx, pfSense.
3. **Do you have a highly available database already?**
   - Yes → point `DB_HOST` at its single-writer endpoint and read
     [Database](building-blocks/database.md) for the contract (single writer, retries).
   - No → start Tier 1 with the single MariaDB you have; add Galera + MaxScale later
     ([Tier 2](tiers.md#tier-2-data-ha)).
4. **Do you have a Vault / OpenBao already?**
   - Yes → `VAULT_URL` to its endpoint, see [Secrets](building-blocks/secrets.md).
   - No → the bundled single Vault works for Tier 1; Raft × 3 is Tier 2.
5. **Do you use the IaC Orchestrator?**
   - Yes → one instance needs the `runner` role (Docker socket, git working trees); route its
     paths to that instance. See [Plugin artifacts & placement](building-blocks/plugins.md).
6. **Do you need the legacy NiceGUI UI?**
   - It is process-bound and needs sticky sessions. Run `LYNDRIX_UI_ENGINE=react` behind a load
     balancer; the preflight warns otherwise. See [Frontend](building-blocks/frontend.md).

## Reading order

- [Tiers](tiers.md) — where you may stop, and what each tier does *not* cover.
- [Architecture & contracts](architecture.md) — instances, leader, leases, placement, bridge.
- [Configuration reference](configuration.md) — every `LYNDRIX_*` variable and the preflight rules.
- Building blocks — one page per layer, same template: contract, options, Lyndrix settings,
  reference setup, verify, known limits.
- [Operations](operations/cluster-status.md) — Cluster page, drills, troubleshooting, backups.
- [For plugin developers](plugin-development.md) — one code path for single and cluster mode.
