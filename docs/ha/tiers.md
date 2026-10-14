# Tiers — where you are allowed to stop

Each tier is a complete, supportable setup. Pick the one that matches your risk appetite; the
higher tiers only add building blocks, they never change how the lower ones work.

| Tier | Name | Runs N× | Remaining single points of failure | Minimum you need |
|---|---|---|---|---|
| **0** | Robust single instance | nothing | everything | backups, `GET /api/ready` health check, restart policy, drain |
| **1** | Application HA | core API, frontend | database, Vault, local storage | load balancer, `LYNDRIX_CLUSTER_MODE=cluster`, same `STORAGE_SECRET` everywhere, identical plugin set, one `runner` instance if you use the IaC Orchestrator |
| **2** | Data HA | + database (Galera, one writer), Vault (Raft) | shared storage (degrades gracefully), the site itself | MaxScale/ProxySQL endpoint, `VAULT_NODES`, shared or object storage |
| **3** | Disaster recovery | backup target on a second site, rehearsed restore | — | off-site dumps/snapshots, written runbook |

## Tier 0 — robust single instance

What it gives you: a core that a load balancer or Docker health check can judge
(`/api/ready`), that stops cleanly (drain) and that you can restore.

Checklist:

- Health check on `GET /api/ready` (never `/api/health`, which calls every plugin).
- `restart: unless-stopped` (Compose) or a Deployment (Kubernetes).
- `LYNDRIX_DRAIN_SECONDS` ≥ two health-check intervals if a proxy sits in front.
- `LYNDRIX_TRUSTED_PROXIES` set to the proxy's address so the login limiter sees real client IPs.
- Backups: `mariadb-dump --single-transaction` plus a Vault snapshot, restore rehearsed once.
  See [Backup & restore](operations/backup-restore.md).

## Tier 1 — application HA

What it gives you: a dead core container is invisible to users (open SSE streams reconnect through
the load balancer), rolling updates without 5xx, and a plugin installed on one instance appearing
on all of them.

What it does **not** cover: the database and Vault are still single. If MariaDB stops, every
instance answers 503 on `/api/ready` until it is back (the core reconnects forever and never
re-bootstraps).

Checklist:

- Two or more core instances with the same `DB_*`, `VAULT_URL`, `LYNDRIX_MASTER_KEY`,
  `STORAGE_SECRET`, and `LYNDRIX_CLUSTER_MODE=cluster` (coordinator `db` is the default there).
- A load balancer implementing the [LB contract](building-blocks/load-balancer.md) — on
  Kubernetes the ingress does.
- `LYNDRIX_UI_ENGINE=react`.
- Identical plugin artifacts: either a shared `/app/plugins` mount or let each instance reconcile
  from the database (default behaviour).
- One instance with role `runner` when the IaC Orchestrator is active; route its paths there.
- Run the preflight (Settings → Cluster → *Re-run checks*): no `FAIL`, understand every `WARN`.

## Tier 2 — data HA

Adds a Galera cluster (three nodes, **one writer** through MaxScale or ProxySQL) and a three-node
Vault/OpenBao with Raft storage. The core needs nothing new for the database beyond `DB_HOST`
pointing at the writer endpoint; the data model assumes a single writer (`SELECT … FOR UPDATE`,
read-modify-write on JSON columns), so a multi-writer Galera setup is explicitly unsupported.

Known limit today: the core unseals only the Vault node it talks to. List all nodes in
`VAULT_NODES` for the preflight health check and unseal the others with Vault's own auto-unseal
until the leader-driven unseal ships. See [Secrets](building-blocks/secrets.md).

## Tier 3 — disaster recovery

Nothing in the core changes. Copy dumps and snapshots to a second site, keep the runbook next to
them and rehearse the restore on a scratch host. [Backup & restore](operations/backup-restore.md)
has the sequence.
