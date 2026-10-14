# Operations — cluster status and preflight

## The Cluster page

Settings → **Cluster** (needs `admin:read`) shows, for the instance that answered the request:

- **This instance**: id, host, roles, leader marker, ready/not ready with reasons, draining,
  uptime, coordinator driver. In single mode a hint explains how to enable cluster mode.
- **Instances**: every row of `cluster_instances` with roles, core/API version, last heartbeat,
  alive/stale, ready/draining; the leader carries a crown.
- **Leases**: name, holder, time to expiry; *Release* (`admin:write`) hands a lease over — the
  peers acquire it within a second. Use it to move the leader or a plugin's background work.
- **Plugin placement**: placement and status per plugin, where its lease is held, `remote`
  markers for pinned plugins.
- **Preflight**: every check with status, message and hint; *Re-run checks* evaluates them now.
- **Coordinator**: TTL, heartbeat, poll interval, bridge counters (published/relayed), drain,
  trusted proxies, database version.

The page refreshes on `cluster:changed` / `cluster:leader_changed` over SSE and every 15 s.

## The API

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://lyndrix.example.com/api/cluster | jq
curl -s -H "Authorization: Bearer $TOKEN" https://lyndrix.example.com/api/cluster/preflight | jq '.checks[] | select(.status != "ok")'
curl -s -X POST -H "Authorization: Bearer $TOKEN" https://lyndrix.example.com/api/cluster/leases/core:leader/release
```

`/api/cluster` is answered by whichever instance the balancer picks; `instance.id` tells you
which. Hit an instance directly (host:8081) to inspect that one.

## Boot screen

In cluster mode the boot screen (React and NiceGUI) lists four extra rows: coordinator (driver,
connecting, failed), leader election (this instance / `<id>` / electing), cluster peers (how
many others are alive) and shared storage (not available in this version).

## Logs to know

| Line | Meaning |
|---|---|
| `CLUSTER: mode=cluster driver=db instance=… roles=…` | startup summary |
| `COORDINATOR: db driver started …` | tables ready, event polling from id N |
| `LEASE: acquired 'core:leader'` / `LEASE: lost '…' to a peer` | lease transitions |
| `LEASE: '…' could not be renewed for 30s — treating it as lost` | coordinator unreachable → fenced |
| `PREFLIGHT [storage_secret] FAIL: …` | contract violation; instance stays out of rotation |
| `DRAIN: SIGTERM received — /api/ready → 503, draining for 10s` | graceful stop in progress |
| `RECONCILE (peer dev-a: installed): {'loaded': […]}` | plugin artifacts converged after a peer's install |
