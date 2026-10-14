# Operations — troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/api/ready` → 503 with `reasons: ["coordinator"]` on one instance | the coordinator could not start (first-boot DDL race, DB unreachable); it retries with back-off | check `CLUSTER: coordinator start failed` in the logs; it recovers by itself once the DB answers |
| `reasons: ["preflight:storage_secret"]` | `STORAGE_SECRET` differs between instances | set the same value everywhere; sessions and SSE tickets of one instance are rejected by the others until then |
| Preflight `roles: FAIL — no live instance has role 'runner'` | the IaC Orchestrator is pinned to `runner` and no live instance has it | start the runner instance / add `runner` to one instance's `LYNDRIX_INSTANCE_ROLES` |
| Plugin page shows a plugin as **Remote** | it is `pinned:<role>` and this instance lacks the role | expected; route its paths to the pinned instance at the balancer |
| `LEASE: '…' could not be renewed for 30s — treating it as lost` | the instance lost the database for a full TTL | fencing worked as designed; the work moved to a peer; check DB connectivity of that host |
| Two instances probe monitors / duplicate state-change events | both hold work that should be leased | a plugin that is not cluster-aware yet, or a lease TTL shorter than the DB outage; check Settings → Cluster → Leases |
| `(1020, "Record has changed since last read")` in plugin logs | MariaDB ≥ 11.6 snapshot isolation + concurrent writers | the core coordinator uses READ COMMITTED + retry; plugins must do the same for maintenance jobs (see the plugin development page) |
| `(1050, "Table 'cluster_instances' already exists")` once at first boot | two instances created the tables together | tolerated; the second instance logs it and continues |
| Browser keeps the old leader's SSE stream after a stop | the drain closed it with `event: reconnect`; EventSource reconnects in 1–3 s | nothing — if streams never reconnect, the balancer buffers responses (fix `flushInterval`/`proxy_buffering`) |
| Login limiter blocks everyone after a few failures | the core sees the balancer's IP | set `LYNDRIX_TRUSTED_PROXIES` (preflight `trusted_proxies` warns) |
| Mixed versions after an update | an instance did not restart | finish the rolling update; preflight `versions` is `warn` until then |
| Boot screen shows *Cluster coordinator: Starting…* forever | DB connected but the coordinator keeps failing | logs of that instance; typically credentials or `max_statement_time` too low for `GET_LOCK` |

## Collecting state

```bash
for h in core-a core-b; do curl -s http://$h:8081/api/ready; echo; done
curl -s -H "Authorization: Bearer $TOKEN" http://core-a:8081/api/cluster | jq '.instances, .lease_table, .preflight.checks'
docker exec <db> mariadb -e "SELECT * FROM cluster_instances; SELECT * FROM cluster_leases; SELECT COUNT(*) FROM cluster_events;" lyndrix_db
```
