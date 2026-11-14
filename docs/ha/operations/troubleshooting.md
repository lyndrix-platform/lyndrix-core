# Operations — troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/api/ready` → 503 with `reasons: ["coordinator"]` on one instance | the coordinator could not start (first-boot DDL race, DB unreachable); it retries with back-off | check `CLUSTER: coordinator start failed` in the logs; it recovers by itself once the DB answers |
| `reasons: ["heartbeat"]` | no heartbeat reached the database for a full lease TTL (dead pooled sockets after a VIP move, a starved event loop/executor, the DB unreachable from this host only) | `GET /api/cluster` on that instance → `coordinator.heartbeat` (`failures`, `last_error`, `last_duration_s`); it clears with the next successful beat. Peers showed the instance as stale during that time |
| Preflight `storage: WARN — … is not responding — a storage job has been stuck for N s` | the `STORAGE_DIR` mount hangs (NFS server / share-manager restart) | fix the mount; the core keeps one thread waiting on it and skips further marker writes meanwhile (`GET /api/cluster` → `storage`). Requests that touch files block until it is back |
| `reasons: ["preflight:storage_secret"]` on one instance | its `STORAGE_SECRET` differs from the value most instances share | set the cluster's value on that instance; the others stay ready and only show `storage_secret: WARN` naming it. To rotate the secret restart all instances together |
| Preflight `roles: FAIL — no live instance has role 'runner'` | the IaC Orchestrator is pinned to `runner` and no live instance has it | start the runner instance / add `runner` to one instance's `LYNDRIX_INSTANCE_ROLES` |
| Plugin page shows a plugin as **Remote** | it is `pinned:<role>` and this instance lacks the role | expected; route its paths to the pinned instance at the balancer |
| `LEASE: '…' was not renewed for 22.5s (ttl 30s) — treating it as lost …` | the instance could not renew the lease (database unreachable or too slow for it) | fencing worked as designed — it stops 7.5 s before a peer can take over; the work moves to a peer after the TTL; check DB connectivity of that host |
| Two instances probe monitors / duplicate state-change events | both hold work that should be leased | a plugin that is not cluster-aware yet, a `lost` callback that does not stop the work, or an instance that was paused past the TTL (the plugin should fence its writes with `ctx.lease_token`); check Settings → Cluster → Leases |
| `(1020, "Record has changed since last read")` in plugin logs | MariaDB ≥ 11.6 snapshot isolation + concurrent writers | the core coordinator uses READ COMMITTED + retry; plugins must do the same for maintenance jobs (see the plugin development page) |
| `(1050, "Table 'cluster_instances' already exists")` once at first boot | two instances created the tables together | tolerated; the second instance logs it and continues |
| Browser keeps the old leader's SSE stream after a stop | the drain closed it with `event: reconnect`; EventSource reconnects in 1–3 s | nothing — if streams never reconnect, the balancer buffers responses (fix `flushInterval`/`proxy_buffering`) |
| Login limiter blocks everyone after a few failures | the core sees the balancer's IP | set `LYNDRIX_TRUSTED_PROXIES` (preflight `trusted_proxies` warns) |
| Mixed versions after an update | an instance did not restart | finish the rolling update; preflight `versions` is `warn` until then |
| Boot screen shows *Cluster coordinator: Starting…* forever | DB connected but the coordinator keeps failing | logs of that instance; typically credentials or `max_statement_time` too low for `GET_LOCK` |
| UI answers 502 on every `/api/*` (Kubernetes), nginx log "could not be resolved" | the UI's nginx resolves the backend via `LYNDRIX_DNS_RESOLVER` (default: Docker's 127.0.0.11) and ignores search domains | set the kube-dns IP and the Service FQDN in `LYNDRIX_BACKEND_URL` ([manifests](../kubernetes/manifests.md)) |
| Vault pod crash-loops with "Failed to lock memory" (Kubernetes) | non-root container without ambient `IPC_LOCK` | `disable_mlock: true` in the Vault config |
| First-boot plugin installs abort with `DetachedInstanceError` | core < 1.4.1 | upgrade; the plugins were still activated, only the post-install verification was skipped |
| One plugin install shows N× "installed" toasts and N² bell entries | core < 1.4.5: every instance's reconcile install notified, and reactions to relayed events were bridged again | upgrade — reconcile installs are silent and derived events stay local |

## Collecting state

```bash
for h in core-a core-b; do curl -s http://$h:8081/api/ready; echo; done
curl -s -H "Authorization: Bearer $TOKEN" http://core-a:8081/api/cluster | jq '.instances, .lease_table, .preflight.checks'
docker exec <db> mariadb -e "SELECT * FROM cluster_instances; SELECT * FROM cluster_leases; SELECT COUNT(*) FROM cluster_events;" lyndrix_db
```
