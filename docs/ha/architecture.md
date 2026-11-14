# Architecture and contracts

```
            DNS ──► load balancer (Traefik / HAProxy / Caddy / nginx / pfSense)   |  k8s: Ingress
                    health = GET /api/ready, no response buffering (SSE)
                                   │
          ┌────────────────────────┼────────────────────────┐
   ┌──────┴──────┐          ┌──────┴──────┐          ┌──────┴──────┐
   │ core api A  │          │ core api B  │          │ core runner │   LYNDRIX_INSTANCE_ROLES
   │ leader      │          │             │          │ api,runner  │   api | leader | runner
   └──────┬──────┘          └──────┬──────┘          └──────┬──────┘
          └────────────────────────┼────────────────────────┘
                 coordinator (driver "db"): cluster_instances · cluster_leases
                 · cluster_kv · cluster_events  →  leader lease, plugin leases,
                 TTL store (OIDC state, SSO codes, limiter), bus bridge + SSE relay
                                   │
                     ┌─────────────┴─────────────┐
               MariaDB (single writer)      Vault / OpenBao
```

## Instances and roles

Every core process is an **instance** with an id (`LYNDRIX_INSTANCE_ID`, default
`<hostname>-<4 hex>`) and a set of **roles** (`LYNDRIX_INSTANCE_ROLES`):

| Role | Meaning |
|---|---|
| `api` | serves HTTP routes; what the load balancer points at |
| `leader` | may hold the core leader lease (seeds, reconcilers, singleton plugins) |
| `runner` | hosts plugins pinned to it (`placement: pinned:runner`), e.g. the IaC Orchestrator with the Docker socket |

Single mode has every role. In cluster mode the default is `api,leader`; give exactly the
instances that have the Docker socket and the git working trees the `runner` role.

Each instance heart-beats into `cluster_instances` every `LYNDRIX_LEASE_HEARTBEAT_SECONDS` with
its roles, versions, readiness and two fingerprints (of `STORAGE_SECRET` and of the loaded plugin
set) that the preflight compares.

Peers see an instance in one of four states (`GET /api/cluster` → `instances[].state`,
`ctx.instance_state()`):

| State | Meaning |
|---|---|
| `alive` | heart-beating within the lease TTL |
| `draining` | heart-beating, SIGTERM received: `/api/ready` is 503, it gets no new shards, its leases are handed over |
| `restarting` | stopped on purpose a moment ago — it left a *tombstone* (the row stays, `stopped_at` set), or it fell silent while draining (killed before it could) — and is expected back within `LYNDRIX_RESTART_GRACE_SECONDS` (90 s) |
| `dead` | no heartbeat for a TTL without a goodbye, or the restart grace is over. Every live instance emits the local event `cluster:instance_lost` once |

A graceful stop used to delete the row at once: the single runner "vanished" for the whole boot
time of its replacement and the IaC Orchestrator reported "No runner instance online" after every
core rollout, while a *crashed* instance stayed visible for a TTL. Now a planned restart keeps the
role `restarting`: `ctx.has_live_role()` stays true and pinned plugins answer 503 with
`Retry-After: 5` and `"role_state": "restarting"` in `X-Plugin-Runtime`.

Every process has an **incarnation** (a boot nonce, `cluster_instances.incarnation`): a restart
under a fixed instance id (`docker-auto-runner`) is a new incarnation with a fresh `started_at`,
and leases of the previous one are never silently inherited (new fencing token). An instance that
cannot read the registry (just started, database unreachable) answers `unknown` about its peers —
never "dead".

## Leases and the leader

A **lease** is "exactly one instance does this". Leases live in `cluster_leases`; acquire and
renew are one atomic `UPDATE … WHERE holder = me OR expires_at < now`, compared on the
**database clock** so instance clock drift cannot steal or extend a lease. A holder renews every
heartbeat, each lease independently of the others. If a renewal does not succeed the holder
**fences itself before the TTL is over**: `lost` fires at `last renewal sent + TTL − margin` on the
holder's monotonic clock (margin 7.5 s by default), i.e. before the row can expire for any peer.
Every acquisition additionally carries a growing **fencing token** (`cluster_leases.token`,
`ctx.lease_token()`), so the late writes of a holder that was paused past its TTL can be rejected
— see [For plugin developers](plugin-development.md#fencing-tokens-api-114).

The core leader is the holder of `core:leader`. It is the only instance that prunes the event
log and dead instance rows; plugins use `ctx.is_leader` for one-shot tasks. Failover: a dead
leader is replaced within one TTL (30 s by default); a leader that stops gracefully releases the
lease and broadcasts it, so a peer takes over within about a second.

Plugins get the same primitive: `ctx.on_lease("monitoring:prober", acquired=start, lost=stop)`.
The State Monitoring plugin runs its probes on exactly one instance this way.

## Placement

A plugin manifest declares where it runs:

| `placement` | `setup()` runs on | background work |
|---|---|---|
| `any` (default) | every instance | wherever the plugin starts it — use `on_lease` for "once" |
| `singleton` | every instance (routes!) | every `ctx.on_lease` binds to one lease `plugin:<id>:singleton` |
| `pinned:<role>` | instances with that role only | other instances answer **503** with `X-Plugin-Runtime: {"phase":"remote"}`; the React shell shows "runs on another instance" |

`pinned` is deliberately simple (no transparent proxying): route the plugin's paths to the pinned
instance at the load balancer.

## Bus bridge and SSE relay

The in-process event bus stays local — every plugin subscribes and emits exactly as before. The
bridge writes an **allowlist** of core topics (`plugin:state_changed`, `plugin:runtime_status`,
`plugin:desired_changed`, `ui:needs_refresh`, `notification:new`, `settings:changed`,
`system:restart_requested`, …) and anything emitted with `cluster=True` into `cluster_events`;
every other instance polls the table (`LYNDRIX_EVENT_POLL_MS`, 500 ms) and re-emits the event
locally with `payload["_origin"] = <sender>`. Because the local SSE forwarders see the re-emitted
event, a browser connected to instance B receives what happened on instance A.

Why an allowlist: the moment a topic is bridged, every side-effecting subscriber runs on N
instances. Plugin topics stay local unless the plugin opts in on purpose.

Two rules keep one action from turning into N × N toasts: an event a handler emits *in reaction
to a relayed event* (synchronously or from the task/thread spawned for it) is never published
again — the originating instance already bridged its own reaction — and installs performed by a
reconcile (boot-time `LYNDRIX_PLUGINS_DESIRED`, cluster convergence) are silent; only the instance
that executed the operator's action emits `plugin:installed` and the "Plugin installed"
notification, which then reaches every browser exactly once through the relay.

## Readiness and drain

`GET /api/ready` is the one contract every load balancer and Kubernetes readiness probe uses. It
is 200 only when boot finished, the database and Vault are connected, the instance is not
draining, the coordinator is up and a heartbeat succeeded within the last lease TTL (cluster mode)
and no hard preflight failure concerns this instance (`storage_secret`). The reasons are listed in
the [configuration reference](configuration.md#readiness).

Heartbeat, lease renewal and preflight are three independent loops. The heartbeat does nothing but
write the registry row and read the peer view at a fixed rate; it never waits for a preflight check
or for the storage mount (the marker file is written by a dedicated thread, one job at a time).

On SIGTERM with `LYNDRIX_DRAIN_SECONDS > 0`, immediately: `/api/ready` flips to 503, one heartbeat
publishes `draining`, and the leases are handed over (`lost` callbacks, a bounded wait for async
ones, release, broadcast) — a peer is leader and runs the leased work about a second later, while
this instance still finishes in-flight requests. Then it waits for the load balancer to notice,
closes every SSE stream with an `event: reconnect`, leaves its tombstone and lets uvicorn shut
down. A second SIGTERM forces an immediate exit. Because the hand-over happens first, a container
runtime that kills the process at the end of the drain no longer leaves leases behind.

## Shared short-lived state

Three things that used to live in process memory now live in `cluster_kv` (in memory in single
mode, same code path): OIDC login state with the PKCE verifier (start on A, callback on B),
one-time SSO exchange codes (callback on B, token exchange on C) and the failed-login counter
(5 failures per minute per user and client IP, cluster-wide). The OIDC callback route is
registered on API-only instances too.

Nothing secret is at rest in that table: an exchange code names only the authenticated identity
and the session token is minted when the SPA redeems it; the PKCE verifier is useless without
the authorization code the IdP sends to the callback; counters are counters. Rows expire
within 60–300 s and the leader prunes them.

## What stays per instance (by design)

- The runtime-status gate (503 while a plugin warms up) is evaluated on the instance that answers;
  the status broadcast carries `instance_id`.
- Plugin React bundles are served by the instance that answers — hence identical plugin artifacts
  everywhere (see [Plugin artifacts](building-blocks/plugins.md)).
- Files under `/data/storage` (attachments, uploads) — until the shared storage driver ships, use
  a shared mount.
