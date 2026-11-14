# Operations — cluster test plan

The cluster has two kinds of tests that are meant to be read as **one plan**:

- **Automated scenarios** (`tests/scenarios/`, part of the default `pytest` run, about 12 s) drive
  2–3 simulated instances against one database with a fake clock. They prove the *logic*: who holds
  which lease when, in which order callbacks fire, what readiness reports.
- **Manual tests** (this page) cover what a simulation cannot: real MariaDB/Galera behaviour, real
  sockets and kernels, real signals, real Docker/Kubernetes timing, real load balancers.

> **Never on production.** Every manual test below breaks something on purpose (kills processes,
> drops packets, seals Vault, takes the database away). Run them **only** on the HA dev bench
> (`docker/docker-compose.ha.dev.yml`) or on a dedicated staging stack with its own database and
> Vault. If you are not sure which stack your shell points at, stop and check.

## 1. Which automated scenario covers which risk

| Scenario test (`tests/scenarios/…`) | Risk it covers | Manual counterpart |
|---|---|---|
| `test_scenario_leader.py::test_graceful_leader_stop_hands_over_at_once_and_lost_comes_first` | a stopping leader releases before its workers stopped, or the successor waits a full TTL | M3 |
| `…::test_crashed_leader_is_replaced_after_the_ttl_and_cannot_act_when_it_returns` | two leaders after a crash/partition; a returning old leader keeps acting | M3, M6 |
| `…::test_paused_instance_fences_itself_before_it_renews` | a frozen process wakes up and runs "exactly-one" work next to the new holder | M3 |
| `…::test_frozen_monotonic_clock_is_caught_by_the_database_at_the_first_renewal` | stale holder after a VM suspend; stale token must be lower than the new one | M3 |
| `…::test_instances_contending_at_the_same_instant_produce_exactly_one_holder` | two instances both think they won a free lease | M1 |
| `test_scenario_db_flap.py::test_a_short_database_flap_costs_nobody_a_lease_and_readiness_heals_itself` | a DB blip moves leases around or leaves a sticky 503 | M1 |
| `…::test_an_outage_longer_than_the_fencing_margin_loses_the_lease_before_a_peer_can_take_it` | overlap of old and new holder during a DB outage | M1, M6 |
| `…::test_a_hung_database_call_is_bounded_and_a_late_answer_does_not_resurrect_the_lease` | heartbeat parked for minutes on a dead socket; a late call revives a lost lease | M1, M2, M6 |
| `test_scenario_runner.py::test_runner_restart_then_grace_expiry_then_return_then_crash` | "No runner instance online" during a runner restart; lost-announcement spam or silence | M5 |
| `…::test_a_second_process_with_the_runner_id_makes_the_older_one_stand_back` | two processes under one `LYNDRIX_INSTANCE_ID` renew each other's leases | M5 |
| `test_scenario_bridge.py::test_an_event_reaches_every_peer_exactly_once_and_never_echoes_back` | duplicated or echoed cluster events | M1 |
| `…::test_events_during_a_database_outage_arrive_in_order_after_the_heal` | events lost or reordered while the DB is away | M1, M6 |
| `…::test_a_long_outage_drops_the_oldest_events_and_counts_them` | silent loss of events in a long outage | M1 |
| `…::test_a_late_committed_lower_id_is_still_delivered_once` | event skipped because a lower id committed late (Galera) | M1 |
| `…::test_oversized_and_secret_looking_payloads_never_reach_the_peers` | a secret persisted in `cluster_events`; a huge payload clogging the table | — |
| `test_scenario_plugins.py::test_plugin_enabled_by_a_peer_starts_publishing_cluster_events_here` | a plugin activated by a peer's event is mute (relay mark leaks) | M4 |
| `…::test_version_that_failed_verification_on_one_instance_is_backed_off_and_reported` | endless download/revert loop of a bad plugin version | M4 |
| `…::test_reload_tears_the_job_down_before_the_lease_moves_to_a_peer` | a peer starts the job while the old one still runs | M3 |
| `…::test_immediate_reenable_gets_the_lease_back_with_a_new_token` | a re-enabled plugin renews a stale holding or never gets the lease | — |
| `test_scenario_readiness.py::test_new_instance_waits_for_its_plugins_and_a_peer_sees_it_ready_only_then` | a new pod takes traffic while its plugins are missing (404s) | M4, M8 |
| `…::test_sigterm_drain_with_three_instances` | 200 on `/api/ready` during the drain; leases released before the work stopped | M3, M8 |
| `…::test_a_wrong_storage_secret_takes_only_the_odd_instance_out_of_rotation` | one bad joiner causes a cluster-wide outage | M4 |
| `test_scenario_rolling_restart.py::test_rolling_restart_goes_one_at_a_time_and_the_leader_last` | two instances down at once; the leader restarted first | M4 |
| `…::test_rolling_restart_aborts_when_an_instance_does_not_come_back` | a rollout that keeps going after one instance failed | M4 |

What the simulation **cannot** show (hence the manual part): the real MariaDB/Galera commit and
lock behaviour (error 1020/1205/1213, out-of-order commits, certification failures), the
wall-clock of the *database* server, kernel TCP behaviour (half-open connections, retransmit
timers), real signals and the Docker/Kubernetes stop sequence, NFS/Longhorn semantics, load
balancer health-check timing, and Vault. One further limit is deliberate: a holder whose
**monotonic** clock stood still (VM suspend) still believes it holds a lease until its first
renewal round (at most one heartbeat interval); the database then refuses it and the old token
is lower than the new one. Downstream consumers that act on a lease must check the fencing token.

## 2. Common set-up

All tests use the dev bench:

```bash
docker compose -f docker/docker-compose.dev.yml -f docker/docker-compose.ha.dev.yml up -d --build
# dev-a: container lyndrix-core-dev   (direct http://localhost:8081, roles api,leader,runner)
# dev-b: container lyndrix-core-dev-2 (direct http://localhost:8082)
# load balancer: http://localhost:8090 (health check GET /api/ready every 3 s)
# database: lyndrix-db-dev, Vault: lyndrix-vault-dev
export TOKEN=...   # an admin API key of the DEV stack
```

Helpers used by the steps (run each in its own terminal):

```bash
# cluster picture of one instance (direct port: 8081 = dev-a, 8082 = dev-b)
watch -n 2 'for p in 8081 8082; do curl -s -H "Authorization: Bearer $TOKEN" localhost:$p/api/cluster \
  | jq -c "{me: .instance.id, ready: .instance.ready, reasons: .instance.reasons, leader: .leader.id, \
            held: .leases.held, states, hb: .coordinator.heartbeat.failures}"; done'
# readiness of both instances and of the balancer
watch -n 1 'for p in 8081 8082 8090; do printf "$p: "; curl -s -o /dev/null -w "%{http_code}\n" localhost:$p/api/ready; done'
# lease and cluster log lines of both instances
docker logs -f lyndrix-core-dev 2>&1 | grep -E "LEASE:|CLUSTER:|COORDINATOR:|BRIDGE:|RESTART:"
docker logs -f lyndrix-core-dev-2 2>&1 | grep -E "LEASE:|CLUSTER:|COORDINATOR:|BRIDGE:|RESTART:"
```

Record the **start time** of each step; most pass criteria are time bounds relative to the
settings of the bench (`LYNDRIX_LEASE_TTL_SECONDS` 30, heartbeat 10, drain 5). Fencing deadline of
a lease = TTL − margin = **22.5 s** with these settings.

Fields used below: `GET /api/cluster` → `instance.ready` / `instance.reasons`, `leader.id`,
`leases.held`, `states` (`alive`/`draining`/`restarting`/`dead` counts), `instances[].state`,
`coordinator.heartbeat` (`failures`, `last_error`, `last_duration_s`, `stale`), `coordinator.bridge`
(`published`, `relayed`, `outbox`, `dropped`, `expired`, `failures`), `storage`, `restart`.

---

## M1 — Database failover (Galera VIP) under load

**Purpose.** Prove that a database node loss — including the nasty variant where the old node is
black-holed instead of refusing connections — does not produce two lease holders or a sticky 503.
Covers the real-socket side of the DB-flap and bridge scenarios.

**Preconditions.** HA dev bench *or* a staging stack with a 2–3 node Galera cluster behind a VIP
(keepalived/HAProxy/MaxScale); both core instances connect through the VIP. Never production.
Generate load: a loop that creates notifications or toggles a setting every second through the
balancer (`:8090`), so events flow.

**Steps.**

1. Note `leases.held` on both instances (the leader holds `core:leader`; a plugin with a lease,
   e.g. monitoring, holds `monitoring:prober`).
2. Variant A (clean): stop the active DB node (`docker stop <node>` / `systemctl stop mariadb`).
3. Variant B (black hole): on the active node `iptables -A INPUT -p tcp --dport 3306 -j DROP`
   (and `OUTPUT … --sport 3306`) instead of stopping it, so connections hang instead of failing.
4. Let the VIP move (or remove the rule after 60 s in the single-node bench; in the bench you
   can approximate the move with `docker stop lyndrix-db-dev; sleep 40; docker start lyndrix-db-dev`).
5. Keep the load running for 2 minutes after the DB is reachable again.
6. Cleanup: remove the iptables rules, start all DB nodes, `SHOW STATUS LIKE 'wsrep_cluster_size'`.

**Expected observations.**

- Logs: `CLUSTER: heartbeat failed (1×, …)` then `heartbeat timed out`/`COORDINATOR: database
  connection pool refreshed (heartbeat timed out)`; for an outage longer than 22.5 s on the holder:
  `LEASE: 'core:leader' was not renewed for 22.5s (ttl 30s) — treating it as lost before a peer can
  take it`; after recovery `CLUSTER: heartbeat recovered after N failure(s)`.
- On every instance the line `LEASE: acquired '<name>'` of the new holder appears **after** the
  `lost` of the old one (compare timestamps across both logs; allow container clock skew < 1 s).
- `/api/ready`: 503 with `reasons: ["database"]` (and/or `["heartbeat"]`) during the outage, 200
  again within one heartbeat interval (≤ 10 s) after the DB answers; no instance stays 503.
- `coordinator.bridge`: `failures` rises during the outage, `outbox` returns to 0, `dropped` and
  `expired` stay 0 for an outage under 5 min (`OUTBOX_MAX_AGE_S`). Compare `events_published` /
  `events_received` (in `coordinator.server`, if exposed) to see whether Galera lost any event.

**Pass.** Exactly one holder per lease at every sampled instant (poll `leases` in the DB:
`SELECT name, holder, expires_at FROM cluster_leases`); `lost` before `acquired` for every moved
lease; all instances ready again ≤ 10 s after the DB is back; `dropped == 0`.
**Fail.** Two holders at the same time, a sticky 503 after recovery, an `acquired` log line of the
new holder before the `lost` of the old one, events missing on the peer.

## M2 — Shared storage (NFS / Longhorn share-manager) hang

**Purpose.** A hung `STORAGE_DIR` mount must neither stop heartbeats nor block the event loop.

**Preconditions.** HA dev bench with `STORAGE_DIR` on an NFS mount (or a staging pod with a
Longhorn RWX volume). Never production storage.

**Steps.**

1. Confirm preflight `storage: ok` (`GET /api/cluster` → `preflight.checks[] | select(.id=="storage")`).
2. Stop the NFS server / scale the share-manager to 0 (`docker stop <nfs>`), leave it down 3 minutes.
3. During the outage: `curl localhost:8081/api/ready`, `curl localhost:8081/api/cluster`, load the UI.
4. Inside the container: `py-spy dump --pid 1` (or `docker top`) and count threads in `D` state.
5. Start the NFS server again; wait 2 minutes.
6. Cleanup: confirm the mount answers (`ls $STORAGE_DIR`), `storage_stuck_s` back to 0.

**Expected.** Heartbeats continue (`coordinator.heartbeat.failures` stays 0, `last_duration_s`
small); `storage.stuck_s` (and the peers' view of `meta.storage_stuck_s`) grows; at most **one**
blocked marker thread; log `CLUSTER: storage job 'marker' has not returned for 120s …`; preflight
`storage: WARN … is not responding — a storage job has been stuck for N s`; `/api/ready` stays 200
unless requests that need files block; `/api/cluster` answers within a second. After the mount
returns the warning clears within one preflight interval (≤ 5 min, or earlier via *Re-run checks*).

**Pass.** No lease loss, no `heartbeat` readiness reason, event loop responsive (UI/`/api/ready`
< 1 s), thread count back to normal. **Fail.** `reasons: ["heartbeat"]`, growing thread count,
`/api/cluster` hanging.

## M3 — Real SIGTERM and SIGKILL, Docker stop timeout

**Purpose.** Verify the drain sequence and the take-over times with the real signal path
(uvicorn, `DrainController`, Docker's stop timeout). The simulation proves the order of callbacks;
only a real container proves that the signal arrives and the process exits in time.

**Preconditions.** HA dev bench (dev-a leads, dev-b stands by, plugins with leases enabled).
`stop_grace_period` of the services ≥ `LYNDRIX_DRAIN_SECONDS` + 10 s (the dev bench: 5 s drain).

**Steps.**

1. **Graceful:** `docker stop lyndrix-core-dev` (SIGTERM). Watch dev-a `/api/ready`, dev-b
   `/api/cluster`, and `time docker stop …`.
2. **Hard:** start dev-a again, wait until it is ready, then `docker kill -s KILL lyndrix-core-dev`.
3. **Stop timeout too short:** `docker stop -t 2 lyndrix-core-dev` (drain is 5 s): the container is
   killed mid-drain.
4. **Pause (frozen process):** `docker pause lyndrix-core-dev` for 50 s (> TTL + margin), then
   `docker unpause`.
5. Cleanup: `docker start` anything stopped; wait for 2 ready instances.

**Expected (1, graceful).** dev-a `/api/ready` → 503 `["draining"]` **immediately** (before the 5 s
drain ends); dev-b sees `instances[dev-a].state == "draining"` after ≤ one beat (≤ 10 s) and holds
`core:leader` + the plugin leases within ~1–2 s of the release; dev-a logs `LEASE: lost` (workers
stopped) → release → `CLUSTER: stopped (drain)`; afterwards dev-b shows dev-a as `restarting`
(tombstone) and no `cluster:instance_lost` until `LYNDRIX_RESTART_GRACE_SECONDS` (90 s) passes.
**(2, hard).** No release: dev-b acquires **after the TTL** (≤ 30 s + one poll), dev-a is `alive`
for ≤ 30 s then `dead`; dev-b logs `CLUSTER: instance dev-a is dead (last heartbeat …)` exactly once.
**(3).** After the kill dev-b behaves as in 2; dev-a was already `draining` in the registry, so it
shows `restarting` (not `dead`) for the grace. **(4).** After `unpause` dev-a logs `LEASE: '…' was
not renewed for …s — treating it as lost` (or `lost … to a peer`) **before** any `acquired`, runs no
plugin job until it re-acquires with a new token, and logs no duplicate side effects.

**Pass.** All timing bounds met; for every moved lease `lost` (old) precedes `acquired` (new);
exactly one `instance_lost` line per observer per death. **Fail.** `/api/ready` 200 during drain,
two holders, a second `instance_lost` for the same incarnation, the old instance running a job
after unpause.

## M4 — Kubernetes rolling update with mixed versions

**Purpose.** A rollout from core 1.11.x to the new version has old and new instances side by side
for minutes: tokens may be missing, the rolling-restart topic is unknown to old instances, plugin
sets differ. Nothing may stop serving.

**Preconditions.** A **staging** k3s namespace (own DB and Vault) with ≥ 3 core pods; the previous
image tag 1.11.x and the new tag available; a runner Deployment if the staging stack has one.
Never the production namespace.

**Steps.**

1. Deploy 1.11.x with 3 replicas; confirm one leader, all ready.
2. Poll continuously from outside: `/api/ready` of each pod IP, one plugin route through the
   service (`curl …/api/plugins/<id>/…`), `GET /api/cluster` on a pod.
3. `kubectl set image deployment/lyndrix-core lyndrix=<new tag>`; `kubectl rollout status`.
4. While one new pod is up next to old pods, trigger *Restart* (rolling restart) from the UI on a
   new pod if it leads; and once with an old pod as leader.
5. Install/upgrade a plugin from a new pod and from an old pod; watch the other side.
6. Misconfigure one new pod on purpose (e.g. `kubectl set env … STORAGE_SECRET=wrong` on a single
   pod via a second Deployment) and observe, then remove it.
7. Cleanup: `kubectl rollout undo` if needed; restore the secret; scale back.

**Expected.** At most `maxUnavailable: 0` pods not ready; new pods answer 503
`["plugins:converging"]` until their plugins are active, then 200; plugin routes never 404 through
the service; preflight `versions: WARN mixed core versions` during the rollout and `ok` after;
`instances[].core_version` shows both; an old pod taking over a lease produces no token (the next
tick of a new pod re-acquires with one — `LEASE: … came back under a new token` is acceptable once);
a rolling restart led by a new pod **skips old instances** (`restart.skipped[…]` says "cannot be
restarted one by one") instead of restarting them all at once; the wrong-secret pod alone shows
`reasons: ["preflight:storage_secret"]` while the others stay ready.

**Pass.** Zero failed requests through the service during the rollout (use a loop with `-f`),
at most one pod unready at any time, one leader at any time. **Fail.** Plugin routes 404 on a
ready pod, two unready pods at once from a "rolling" restart, all pods unready because of one odd
pod.

## M5 — Runner host reboot with a job in flight

**Purpose.** The IaC orchestrator runs jobs on the `runner` instance. A reboot of that host must
not report "no runner online" while it is coming back, must not run the job twice, and must not
leave a job `running` forever.

**Preconditions.** HA dev bench where dev-a has the `runner` role (default) and the orchestrator
plugin is active against a harmless target (a dry-run/`--check` pipeline or a sleep job); dev-b is
`api` only. Never a real infrastructure pipeline.

**Steps.**

1. Start a long dry-run job (≥ 3 min) from the orchestrator UI via the balancer.
2. Variant A (graceful): `docker restart lyndrix-core-dev` (compose recreate behaves the same).
3. Variant B (host reboot): reboot the dev VM / `echo b > /proc/sysrq-trigger` on the **dev** host
   (no SIGTERM, no goodbye).
4. Poll `GET /api/cluster` on dev-b and the orchestrator's job list through the balancer.
5. Wait for the runner to come back; re-run a job.
6. Cleanup: cancel leftover jobs; confirm `states.alive == 2`.

**Expected.** A: dev-b shows `instances[dev-a].state == "restarting"` (and orchestrator routes answer
503 with `X-Plugin-Runtime` `role_state: restarting`, `Retry-After: 5`, **not** "no runner
online"); the instance returns as a **new incarnation** (`instances[].incarnation` changed,
`started_at` reset); the interrupted job is marked failed/aborted by the orchestrator (never
silently `running`). B: dev-a is `alive` for ≤ 30 s, then `dead` (log `CLUSTER: instance dev-a is
dead`, once), the role is `dead` until it returns; after the return the next crash is announced again.
If the downtime exceeds 90 s even variant A ends in `dead` with `graceful: true` in the event.

**Pass.** No "No runner instance online" during the restart window of variant A; exactly one
`instance_lost` per death per observer; the in-flight job ends in a terminal state; the job is not
executed twice. **Fail.** The UI says no runner while `restarting`; the job stays `running`
after the runner is back; the runner id conflict (`instance_id_conflict`) appears after a recreate.

## M6 — Network partition between sites

**Purpose.** An instance cut off from the database (or from its peers) must fence itself, and the
side with the database must take over — without overlap.

**Preconditions.** Staging with core instances in two "sites" (two Docker hosts or two k3s nodes)
and the database in site 1; or on the dev bench: `docker network disconnect <net> lyndrix-core-dev-2`.
Never production.

**Steps.**

1. dev-b (site 2) holds a plugin lease; note `leases.held` on both.
2. **Asymmetric partition:** drop only dev-b → DB: `docker exec lyndrix-core-dev-2 iptables -A OUTPUT -p
   tcp --dport 3306 -j DROP` (needs `NET_ADMIN`) — dev-b still serves users via the balancer.
3. Keep it for 60–90 s; then remove the rule.
4. **Full partition:** `docker network disconnect <net> lyndrix-core-dev-2` for 90 s, reconnect.
5. Cleanup: remove rules, reconnect networks, wait for 2 ready instances.

**Expected.** dev-b: heartbeat failures, `LEASE: '<name>' was not renewed for 22.5s … treating it as
lost before a peer can take it` at ~22 s, `/api/ready` 503 `["heartbeat"]` from 30 s (the balancer
removes it), its plugin job stops (`lost`); dev-a acquires only after ~30 s (`acquired` after dev-b's
`lost`); after the heal dev-b's beat recovers, it stays out of leases already held, `/api/ready`
200; peers show dev-b `alive` again with the same incarnation (no `instance_lost` was emitted for a
pure DB partition seen from the DB side beyond the TTL: one announcement is acceptable, a second for the
same incarnation is not).

**Pass.** No overlap (`lost` on dev-b before `acquired` on dev-a); dev-b out of the balancer within
one health-check interval after 30 s; no user-visible 5xx burst on dev-a. **Fail.** Both instances
run the plugin job at the same time; dev-b keeps answering 200 on `/api/ready` for minutes.

## M7 — Vault re-seal

**Purpose.** A sealed Vault must make instances unready (secrets unreachable) and recover without
a restart when it is unsealed.

**Preconditions.** HA dev bench (`lyndrix-vault-dev`), a throw-away Vault with the dev unseal
keys. Never a shared or production Vault.

**Steps.**

1. `docker exec lyndrix-vault-dev vault operator seal` (token required), wait 60 s.
2. Observe `/api/ready` on both instances, the UI login, plugin actions that read secrets.
3. Unseal (`vault operator unseal` ×threshold, or restart with `LYNDRIX_MASTER_KEY` auto-unseal).
4. Cleanup: confirm `vault status` unsealed; both instances ready.

**Expected.** `/api/ready` 503 with `reasons: ["vault"]` on both instances within the health-check
period; leases and heartbeats are **unaffected** (they live in the database) — `leader.id` does not
change and no `LEASE: lost` appears; after the unseal both return to 200 without a restart
(`vault:ready_for_data` is emitted again; plugins guarded by it resume).

**Pass.** Readiness follows Vault state in both directions; no lease movement caused by Vault.
**Fail.** Instances stay 503 after the unseal, or leadership flaps while Vault is sealed.

## M8 — Load balancer behaviour on `/api/ready`

**Purpose.** The balancer contract (`docs/ha/building-blocks/load-balancer.md`) — health check on
`/api/ready` only, drain-aware, no buffering of SSE — works with the real balancer.

**Preconditions.** HA dev bench behind the dev Traefik `:8090` (health check every 3 s), or the
staging balancer (HAProxy/nginx/Traefik) configured per the docs. Never the production balancer.

**Steps.**

1. `while true; do curl -s -o /dev/null -w "%{http_code} %{header_json}\n" localhost:8090/api/cluster | …; sleep 0.5; done`
   with a header that shows which backend answered (`instance.id` from the body).
2. Open an SSE stream through the balancer (`curl -N …/api/events?ticket=…`) and keep it open.
3. `docker stop lyndrix-core-dev-2` (SIGTERM, drain 5 s): watch when the backend disappears from
   the rotation relative to `/api/ready` flipping to 503.
4. Start it again: watch when it re-enters the rotation (should be after `/api/ready` is 200, not before).
5. Start a **new** instance/pod with an empty plugin directory (`plugins:converging`): it must not
   receive traffic until ready.
6. Make one instance unready on purpose (`STORAGE_SECRET` mismatch) — only it leaves the rotation.
7. Cleanup: restore the instance; close the SSE stream.

**Expected.** During the drain the balancer stops routing to the draining instance within one
health-check interval (≤ 3 s + 1 failure threshold) while in-flight requests finish (drain 5 s);
SSE clients of the stopped instance reconnect through the balancer to the other one; a starting
instance gets no requests while `reasons` is non-empty; a request through the balancer never
reaches an instance that answered 503 on `/api/ready` for longer than the check interval.

**Pass.** Failed client requests during a graceful stop ≈ 0 (at most those in the one check
interval before the 503 was seen); no traffic to unready instances thereafter; SSE reconnects.
**Fail.** The balancer keeps routing to a draining instance for the whole drain; a converging pod
serves 404s; one odd instance takes all instances out.

---

## Recording results

For each run note: date, core version(s), bench vs staging, the measured times (outage start,
`lost`, `acquired`, ready again), and the pass/fail verdict. A failure that the simulation did not
predict is a **missing automated scenario** — add a story to `tests/scenarios/` (fake clock, same
harness `tests/cluster/cluster_harness.py`) and reference it in the table of section 1.
