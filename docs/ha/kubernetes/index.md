# Kubernetes — overview

Kubernetes already provides what Docker needs extra parts for: readiness-driven routing
(Ingress + Service + `readinessProbe`), rolling updates with surge/unavailable budgets, and
operators for the database and Vault. The core sees exactly the same environment variables as on
Docker — nothing in it is Kubernetes-specific.

## Object model

| Object | Purpose | Settings |
|---|---|---|
| `Deployment lyndrix-core` (replicas 2–3) | API instances | `LYNDRIX_CLUSTER_MODE=cluster`, `LYNDRIX_INSTANCE_ROLES=api,leader`, `LYNDRIX_UI_ENGINE=react`, `LYNDRIX_TRUSTED_PROXIES=*` (pods see the ingress' pod IP), `LYNDRIX_DRAIN_SECONDS=10`, `LYNDRIX_INSTANCE_ID` from `metadata.name` via the downward API |
| `Deployment lyndrix-runner` (replicas 1) | IaC Orchestrator host | `LYNDRIX_INSTANCE_ROLES=api,runner`; `nodeSelector` to a node with Docker (or DinD) until runners become Kubernetes Jobs |
| `Deployment lyndrix-ui` | frontend | `LYNDRIX_BACKEND_URL=http://lyndrix-core` (the Service) |
| `Service lyndrix-core` | load balancing | ClusterIP; readiness decides membership |
| `Ingress` | TLS, routing | one host; path rule for the runner's plugin prefix → `lyndrix-runner` Service |
| `Secret` | `STORAGE_SECRET`, `LYNDRIX_MASTER_KEY`, `vault_keys.enc`, DB password | mounted/env on every pod |
| `PersistentVolumeClaim` RWX (Longhorn, NFS) | `/data/storage` | shared files until the storage driver ships |
| `PodDisruptionBudget` | keep ≥ 1 ready during node drains | `minAvailable: 1` |

Probes:

```yaml
livenessProbe:  { httpGet: { path: /api/live,  port: 8081 }, periodSeconds: 10 }
readinessProbe: { httpGet: { path: /api/ready, port: 8081 }, periodSeconds: 5, failureThreshold: 2 }
startupProbe:   { httpGet: { path: /api/live,  port: 8081 }, failureThreshold: 30, periodSeconds: 5 }
terminationGracePeriodSeconds: 30    # ≥ LYNDRIX_DRAIN_SECONDS + 15
```

## Database and Vault on Kubernetes

- Database: the MariaDB operator (`MariaDB` CRD with `galera:` and a `MaxScale` CRD) gives
  Galera with a single-writer endpoint, backups as CRDs and rolling updates; or an external
  database via `DB_HOST`. Contract: [Database](../building-blocks/database.md).
- Vault/OpenBao: the official Helm charts with Raft (`server.ha.raft`) on a block storage class;
  or an external Vault. Contract: [Secrets](../building-blocks/secrets.md).

## Known limits

- No Helm chart or Kustomize base is shipped yet; the object model above is the spec for it.
- Orchestrator runners need a Docker daemon; running them as Kubernetes Jobs is a redesign that
  is out of scope for this milestone.

See also [Ingress & load balancing](ingress.md) and [Rolling updates](rolling-updates.md).
