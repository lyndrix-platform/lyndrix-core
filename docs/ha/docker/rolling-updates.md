# Docker — rolling updates

With two or more instances behind a load balancer a core update needs no downtime.

## Sequence

1. **Runner first** (if you have one): its plugin routes are pinned there anyway, so a short
   absence is visible only in the IaC Orchestrator UI. `docker compose pull && docker compose up -d`
   on the runner host; wait for `GET /api/ready` → 200.
2. **API instances one by one**: pull, `up -d`, wait for `/api/ready` on that host, then the next.
   During the window the preflight shows `versions: warn` (mixed versions) — expected.
3. Verify in Settings → Cluster: every instance alive, one version, preflight without `FAIL`.

What happens on each `docker stop`:

- SIGTERM → `/api/ready` answers 503 → the balancer removes the instance after its next check
  (3–5 s) → after `LYNDRIX_DRAIN_SECONDS` the instance closes SSE streams with `event: reconnect`
  (browsers reconnect through the balancer), releases its leases (a peer takes the leader lease
  and plugin leases within about a second) → uvicorn shuts down.
- `stop_grace_period` must be larger than drain + graceful shutdown or Docker kills the process.

## Schema changes

DDL runs at boot under `GET_LOCK('lyndrix_bootstrap')`: the first new instance applies additive
changes while the old ones keep running. Only additive migrations (new tables, new nullable
columns) are safe for a rolling update — that is what the core and the shipped plugins do.

## Rolling restart without an update

`POST /api/system/restart` (Settings → System Info → *Restart*) in cluster mode broadcasts
`system:restart_requested`: every instance drains and exits in a staggered order (5 s apart,
leader last) and the restart policy brings it back. No Docker socket is needed on the API
instances.

## Rollback

Re-deploy the previous image tag with the same sequence. Database changes are additive, so an
older core runs against a newer schema.
