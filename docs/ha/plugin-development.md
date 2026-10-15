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
    async with ctx.lease("myplugin:nightly") as held:  # one-shot lease, not renewed
        if held:
            await run_report()

ctx.is_leader          # True while this instance holds core:leader (always True in single mode)
ctx.instance_id        # LYNDRIX_INSTANCE_ID
ctx.cluster_size       # live instances (1 in single mode)
ctx.roles              # LYNDRIX_INSTANCE_ROLES of this instance
ctx.cluster_mode       # "single" | "cluster"
ctx.cluster_instances  # [{"instance_id", "hostname", "roles", "alive", "is_self"}, ...] (API ≥ 1.7.1)
ctx.has_live_role("runner")   # does any live instance carry the role? → report "no executor online"
ctx.emit("myplugin:items_changed", {...}, cluster=True)   # reaches every instance

@ctx.subscribe("plugin:settings_changed")                 # core ≥ 1.5.1, bridged: fires on every
async def _settings(payload):                            # instance when the operator saves settings
    if payload.get("plugin_id") == manifest.id:          # ({"plugin_id", "keys", "by"} — never values)
        svc.reload_settings()                            # re-read ctx.get_setting()/Vault here
```

### Leases

- `acquired` fires when this instance obtains the lease (immediately in single mode), `lost`
  when a peer took it over or the coordinator was unreachable for a full TTL. **Stop the work in
  `lost`.** Callbacks may be sync or async.
- `dispose()` (unload/disable) forgets the plugin's leases and releases them so a peer takes
  over at once.
- `placement="singleton"` binds every `on_lease` call of the plugin to one lease
  `plugin:<id>:singleton` — use it when the plugin's background work is one unit.
- `ctx.lease(name)` is a one-shot: it tries once for the TTL and releases on exit; if the keeper
  already holds the lease it reports `held=True` without touching it.

### Cluster events

`ctx.emit(topic, payload, cluster=True)` writes the event to the shared log; peers receive it on
their local bus with `payload["_origin"] = <sender instance id>`. Use it for "my data changed,
re-read it" — **never** to fan work out N times. Your own emit still reaches your own subscribers
without `_origin`, so check it before reacting to avoid doing the work twice.

### Placement

- `any`: default. Routes run everywhere; guard background work with `on_lease`.
- `pinned:<role>`: `setup()` runs only where `LYNDRIX_INSTANCE_ROLES` contains the role; other
  instances mount a 503 stub (`X-Plugin-Runtime: {"phase": "remote", "role": …}`) and the shell
  shows "runs on another instance". Use it for host-bound plugins (Docker socket, local git
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
