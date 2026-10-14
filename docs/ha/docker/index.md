# Docker — overview and host layouts

Docker Compose has no scheduler and no readiness-driven routing, so HA on Docker is built from
explicit parts: N core stacks on N hosts, **a separate load balancer**, and (Tier 2) a database
and Vault stack per host.

## Host layouts

| Layout | Hosts | What runs where | Trade-off |
|---|---|---|---|
| **Three hosts** | 3 | core (`api,leader`) on each; `runner` on one; Galera node + local MaxScale + Vault Raft node on each (Tier 2) | clean quorum for Galera and Raft |
| **Two hosts + arbiter** | 2 (+ small) | core on both; Galera needs a `garbd` arbiter and Raft a third voter on the small host | fits two-node setups |
| Dedicated LB pair | +2 small LXCs | HAProxy/Traefik with keepalived VIP | cleanest separation |
| Co-located LB | 0 extra | LB on every app host, VIP wanders | fewer hosts, LB and app fail together |
| Appliance LB | 0 extra | pfSense HAProxy package + CARP VIP | no container to run |

## Tier 1 on Docker in five steps

1. Put the same `docker-compose.yml` (core image, no bundled DB/Vault) on each host. Point
   `DB_*`, `VAULT_URL`, `LYNDRIX_MASTER_KEY`, `STORAGE_SECRET` at the shared services; set
   `LYNDRIX_CLUSTER_MODE=cluster`, `LYNDRIX_INSTANCE_ID`, `LYNDRIX_INSTANCE_ROLES`
   (`api,leader`, plus `runner` on the host with the Docker socket mount), `LYNDRIX_UI_ENGINE=react`,
   `LYNDRIX_TRUSTED_PROXIES=<LB address>`, `LYNDRIX_DRAIN_SECONDS=10`.
2. Mount the Vault key file (`/data/security/vault_keys.enc`) on every host (secret, not storage).
3. Share `/data/storage` (NFS/CIFS) or accept that uploads are instance-local for now.
4. Deploy the load balancer with the [contract](../building-blocks/load-balancer.md) and the
   routing rule for the runner's plugin paths.
5. Start the instances one by one; check Settings → Cluster (or `GET /api/cluster`) for two live
   instances and a clean preflight.

## The dev test bench

`docker/docker-compose.ha.dev.yml` on top of the dev stack runs two cores (`dev-a` with
`api,leader,runner`, `dev-b` with `api,leader`) behind the dev Traefik on `http://localhost:8090`
with the health check on `/api/ready`. It is the mandatory proving ground for every cluster
change — see [Compose reference](compose-reference.md).

Pages in this branch: [the separate load balancer](load-balancer.md), [Compose
reference](compose-reference.md), [rolling updates](rolling-updates.md).
