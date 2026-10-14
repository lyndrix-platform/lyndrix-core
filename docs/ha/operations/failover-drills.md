# Operations — failover drills

Run these on the dev test bench first (`docker/docker-compose.ha.dev.yml`), then on staging.
Each drill names what to watch and the result measured on the test bench (two instances, lease
TTL 30 s, heartbeat 10 s, drain 5 s).

## 1. Kill the leader gracefully

```bash
docker stop lyndrix-core-dev-2        # the instance holding core:leader (+ monitoring:prober)
watch -n 2 'curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8081/api/cluster | jq -c "{leader: .leader.id, alive, held: .leases.held}"'
```

Expected: `/api/ready` of the stopped instance is 503 during the drain, the balancer stops
routing to it, the other instance holds `core:leader` and `monitoring:prober` within seconds and
starts probing (monitoring health `prober: true`, scheduler jobs > 0). Measured: leader and prober
moved about 10 s after the stop (one heartbeat interval; with the release broadcast it is about
one second), probing resumed after the bootstrap sweep ~15 s later.

## 2. Kill the leader hard

```bash
docker kill lyndrix-core-dev-2
```

Expected: no release broadcast — the lease expires after the TTL; take-over within
`LYNDRIX_LEASE_TTL_SECONDS` (30 s). Open SSE streams of that instance drop and reconnect through
the balancer.

## 3. Install a plugin on A, look at B

Install (or enable/disable) a plugin on instance A; `GET /api/plugins` on B shows the change
within a few seconds (`RECONCILE` log line on B). The preflight `plugin_artifacts` check is `ok`
again once both sets match.

## 4. Change a setting on A

Change the log level or theme in Settings on A; B logs `CONFIG: settings re-hydrated after a
change on instance dev-a` and applies it.

## 5. SSO across instances (if OIDC is configured)

Start the login on A (stop A right after the redirect to the IdP), finish the callback on B: the
PKCE state is read from `cluster_kv`, the exchange code is redeemed on whichever instance the
SPA hits.

## 6. Database outage

```bash
docker stop lyndrix-db-dev; sleep 60; docker start lyndrix-db-dev
```

Expected: every instance answers 503 on `/api/ready` (`reasons: ["database"]`), keeps retrying
with back-off forever, reconnects without re-running the IAM bootstrap or loading modules twice
(`STARTUP: IAM re-verified after a database reconnect`), and goes back to 200.

## 7. Rolling restart from the UI

Settings → System Info → *Restart* in cluster mode: instances exit staggered (5 s apart, leader
last) and come back; the balancer never runs out of ready instances with ≥ 2 instances.

## 8. Load balancer failover (production)

Stop the active balancer; the VIP moves (keepalived/CARP) and clients reconnect. Not covered by
the dev bench.
