# For plugin developers

One code path runs in single and cluster mode. In single mode the coordinator driver is `local`:
every lease is granted to the only instance and `cluster=True` is a no-op — your plugin behaves
exactly as before.

## The API (core API ≥ 1.7)

```python
from core.api import ModuleManifest

manifest = ModuleManifest(
    ...,
    placement="any",            # any | singleton | pinned:<role>
    permissions={
        "emit": ["myplugin:items_changed"],
        "subscribe": ["db:connected", "myplugin:items_changed"],
    },
)

def setup(ctx):
    ctx.register_routes(router)                       # runs on EVERY instance (reads/writes via DB)

    # exactly one instance runs the background work
    on_lease = getattr(ctx, "on_lease", None)         # stay loadable on older cores
    if callable(on_lease):
        on_lease("myplugin:worker", acquired=svc.start_worker, lost=svc.stop_worker)
    else:
        svc.start_worker()

    @ctx.subscribe("myplugin:items_changed")
    async def _changed(payload):
        if payload.get("_origin"):                    # came from a peer
            svc.reload()

async def nightly(ctx):
    async with ctx.lease("myplugin:nightly") as held:  # one piece of work; renewed while it runs
        if held:
            await run_report()

ctx.is_leader          # True while this instance holds core:leader (always True in single mode)
ctx.instance_id        # LYNDRIX_INSTANCE_ID
ctx.cluster_size       # live instances (1 in single mode)
ctx.roles              # LYNDRIX_INSTANCE_ROLES of this instance
ctx.cluster_mode       # "single" | "cluster"
ctx.cluster_instances  # [{"instance_id", "hostname", "roles", "alive", "is_self",
                       #   "state", "incarnation", "heartbeat_at", "age_s"}, ...]  ([] = view not ready)
ctx.has_live_role("runner")   # False only when the role is known to be gone → "no executor online"
ctx.role_state("runner")      # API ≥ 1.14: {"role", "state": alive|draining|restarting|dead|unknown, "instances"}
ctx.instance_state(iid)       # API ≥ 1.14: {"state", "known", "roles", "incarnation", "last_heartbeat", "age_s"}
ctx.incarnation               # API ≥ 1.14: boot nonce of this process (new on every start)
ctx.emit("myplugin:items_changed", {...}, cluster=True)   # reaches every instance

@ctx.subscribe("plugin:settings_changed")                 # core ≥ 1.5.1, bridged: fires on every
async def _settings(payload):                            # instance when the operator saves settings
    if payload.get("plugin_id") == manifest.id:          # ({"plugin_id", "keys", "by"} — never values)
        svc.reload_settings()                            # re-read ctx.get_setting()/Vault here
```

### Sharded work / load balancing (API ≥ 1.8)

Work that can be spread over several instances — probing 200 hosts, polling 40 Docker hosts —
does not need a lease per item. Ask the cluster which instances run a named workload and which
share of the keys is yours:

```python
workers = ctx.cluster_workers("probe", count=2, roles=["runner"])   # ["docker-auto-runner", "…"]
mine    = ctx.my_shard("probe", monitor_ids, count=2, roles=["runner"])  # my monitors
ctx.is_worker("probe", count=2, roles=["runner"])                   # am I one of them?
ctx.cluster_shard("probe", monitor_ids)                              # {instance_id: [ids]} over every candidate
```

- Every instance computes the **same answer locally** from the live instance list (rendezvous
  hashing) — no round trip; only the keys of an instance that left or joined move.
- `roles` filters the candidates (`None` = every live instance), draining instances are skipped,
  `count` is capped by the live candidates; single mode answers with this instance.
- Recompute on `cluster:changed` (membership) and apply the difference (start/stop work).
  Results are at-least-once while heartbeat views differ for a few seconds — use a lease for
  exactly-once singletons.
- Settings → Cluster → *Work assignments* shows every workload asked about, its workers and
  key counts.

### Leases

- `acquired` fires when this instance obtains the lease (immediately in single mode), `lost`
  when a peer took it over or the lease could not be renewed in time. **Stop the work in
  `lost`.** Callbacks may be sync or async.
- **`lost` fires before a peer can hold the lease** (core ≥ 1.12). The holder fences itself on its
  own monotonic clock at `last successful renewal + TTL − margin` (margin = a quarter of the TTL,
  at most one heartbeat interval, at least 2 s — 7.5 s with the default 30 s / 10 s), measured from
  the moment the renewal was *sent*. A watchdog fires at that deadline whatever the renewals are
  doing; a hung database call delays neither the other leases nor the fencing. Keep `lost` quick:
  the margin is the time your worker has to stop.
- On a hand-over (`ctx.release_lease`, drain, the admin *Release* button) the order is: `lost`
  callbacks → wait for async ones (at most 5 s) → release → a peer's `acquired`.
- A `ttl=` below two heartbeat intervals is raised to that minimum (logged) — a shorter lease
  would expire between two renewals.
- `dispose()` (unload/disable) forgets the plugin's leases and releases them so a peer takes
  over at once.
- `placement="singleton"` binds every `on_lease` call of the plugin to one lease
  `plugin:<id>:singleton` — use it when the plugin's background work is one unit.
- `ctx.lease(name)` is a lease for one piece of work: it tries once, is **renewed while the block
  runs** (core ≥ 1.12 — before, work longer than the TTL lost the lease without noticing) and is
  released on exit. It is exclusive within one instance too: a second concurrent scope for the
  same name gets `held=False`. If `ctx.on_lease` already holds the lease the scope reports
  `held=True` without touching it. In a long body check `ctx.holds_lease(name)` now and then and
  fence writes with `ctx.lease_token(name)`.
- `ctx.release_lease(name)` (API ≥ 1.9) withdraws an `on_lease` interest at runtime. A held lease
  is released at once, the plugin's `lost` callback fires (one "stop the work" path) and the
  release is broadcast so a peer acquires it within about a second. Use it when a setting such as
  monitoring's `probe_roles` excludes this instance — no plugin reload needed.

### Who is there — and what became of an instance (API ≥ 1.14)

An instance is `alive`, `draining`, `restarting` (stopped on purpose, expected back within
`LYNDRIX_RESTART_GRACE_SECONDS`) or `dead`; `unknown` means the instance you are asking on has no
usable view of the registry right now (just started, database unreachable).

- `ctx.has_live_role(role)` is `False` only for `dead`. During a planned restart of the only
  runner it stays `True` — queue the work, the runner picks it up when it is back. Show "runner
  restarting" from `ctx.role_state(role)["state"]` if you want to be precise.
- **Orphaned work** (a job claimed by an instance that died): stamp the claim with
  `ctx.instance_id` **and** `ctx.incarnation`. A sweeper (run it under `ctx.on_lease`) treats a
  claim as orphaned when `ctx.instance_state(claimed_by)["state"] == "dead"`, or when the instance
  is alive again under a *different* incarnation — plus your own "no progress since …" criterion.
  Never act on `restarting` or `unknown`. Fence the takeover with the sweeper's lease token and let
  the returning runner check the job row before every status change.
- `@ctx.subscribe("cluster:instance_lost")` (declare it in `permissions.subscribe`) is emitted
  **locally on every live instance, once** per dead peer —
  `{instance_id, roles, last_heartbeat, incarnation, graceful, observed_by}`; not for a restart
  within the grace, again if the peer returns and dies later. Use it as a trigger and still
  decide from `ctx.instance_state()` / your database (several instances see the same death).
- `cluster:changed` with `reason: "instances"` carries `joined`, `left`, `restarting`, `lost`.

### Fencing tokens (API ≥ 1.14)

The deadline above stops the *keeper*. It cannot stop work that is already running in a thread or
a subprocess while the event loop is stalled (a blocking call on the loop, a frozen container): the
paused holder wakes up after the TTL, a peer has taken over, and the old work still writes. For
that case every acquisition of a lease carries a **token that only grows** (per lease name,
cluster-wide, also across restarts and releases):

```python
def setup(ctx):
    ctx.on_lease("myplugin:worker", acquired=svc.start, lost=svc.stop)

class Service:
    def start(self, token=None):          # gets the token because the parameter is named "token"
        self.token = token                # None on cores < 1.14 → no fencing, as before
    ...
    def write(self, session, row_id, values):
        token = self.ctx.lease_token("myplugin:worker")      # None once this instance is fenced
        if token is None:
            return False                                     # not the holder any more → stop
        updated = session.execute(
            update(State).where(State.id == row_id, State.lease_token <= token)
            .values(**values, lease_token=token)
        ).rowcount
        return bool(updated)              # 0 rows: a newer holder wrote already → I am stale
```

- `acquired` receives the token only when the callback declares a parameter named `token` (or has
  one required positional parameter); existing callbacks without parameters keep working unchanged.
- `ctx.lease_token(name)` returns the token while this instance holds the lease, else `None`.
  Call it per write (it is a dict lookup, thread-safe) rather than caching it for long.
- Compare with `<=` and store the token with the row; never compute with it — it is opaque
  (today: a counter that never drops below the database clock in microseconds).
- Side effects outside the database (ICMP, Docker, GitLab, Discord) cannot be fenced this way —
  make them idempotent or record "done by token N" first.
- Mixed versions: an instance of core ≤ 1.11 that takes a lease over issues no token. The next
  acquisition by a newer instance still gets a larger one; only while such an old instance *holds*
  the lease are its writes unfenced.

### Cluster events

`ctx.emit(topic, payload, cluster=True)` writes the event to the shared log; peers receive it on
their local bus with `payload["_origin"] = <sender instance id>`. Your instance's events arrive in
the order you emitted them and survive a short database outage (they are queued and sent
afterwards); keep payloads small — above 64 kB an event is not published. Use it for "my data changed,
re-read it" — **never** to fan work out N times. Your own emit still reaches your own subscribers
without `_origin`, so check it before reacting to avoid doing the work twice.

Do not emit a bridged event *as a direct reaction* to a relayed one (`payload["_origin"]` set) and
expect it to travel on: reactions to relayed events stay local by design. Work you start with
`ctx.create_task`, your `setup()` and your lease callbacks are not reactions — whatever they emit
later is published normally, no matter what caused the plugin to be loaded.

`ctx.emit` may be called from a worker thread (core ≥ 1.7): the bus hands the call over to the
application loop with `call_soon_threadsafe`, so subscribers — sync and async — always run on the
loop. Older cores needed a manual `loop.call_soon_threadsafe` hop.

### Placement

- `any`: default. Routes run everywhere; guard background work with `on_lease`.
- `pinned:<role>`: `setup()` runs only where `LYNDRIX_INSTANCE_ROLES` contains the role; other
  instances mount a 503 stub (`X-Plugin-Runtime: {"phase": "remote", "role": …, "role_state": …,
  "retry_after": …, "message": …}`) and the shell shows "runs on another instance". `role_state`
  says whether the pinned role is `alive` (a load-balancer route is missing — `Retry-After: 30`),
  `draining`/`restarting` (planned restart — `Retry-After: 5`) or `dead`. Use it for host-bound plugins (Docker socket, local git
  trees). Pydantic ignores the field on older cores.

## Rules for cluster-safe plugins

1. **No in-process state that another instance needs.** Caches are fine; the source of truth is
   the database (or Vault). Request/reply over the bus (`ctx.request`) stays local on purpose —
   responder plugins run on every instance.
2. **Idempotent, lock-aware DDL.** `create_all(checkfirst=True)` + `ADD COLUMN IF NOT EXISTS`; wrap
   in `db_instance.advisory_lock("lyndrix_bootstrap")` when available (`getattr`) so N booting
   instances serialize. Never run DDL in `health()`.
3. **Expect MariaDB error 1020** ("record has changed since last read", snapshot isolation) when
   writers on two instances touch the same rows: use short READ COMMITTED transactions, retry
   1020/1205/1213, stop your own writers before destructive maintenance (see the monitoring
   plugin's `quiesce_monitors`), and never let one lost write kill a scheduler job.
4. **Every table has a primary key** (Galera).
5. **Files belong on shared storage** (or in the DB) if another instance must serve them.
6. **Keep `health()` cheap** and make it report standby correctly — a standby instance without a
   scheduler is healthy, not broken (monitoring reports `prober: false`).
7. **Announce changes** other instances must react to with `cluster=True` and declare the topic
   in both `permissions.emit` and `permissions.subscribe`.

## Reference implementations

- `plugins/lyndrix-plugin-monitoring`: `on_lease("monitoring:prober")`, `start_probing` /
  `stop_probing`, `monitoring:monitors_changed` with `cluster=True`, 1020-tolerant probe writes,
  standby-aware `health()`.
- `plugins/lyndrix-plugin-iac-orchestrator`: `placement="pinned:runner"`.
- Core tests: `tests/cluster/test_context_cluster.py`, `tests/cluster/test_placement.py`.
