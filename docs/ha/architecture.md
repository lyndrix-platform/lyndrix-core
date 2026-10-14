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
set) that the preflight compares. An instance whose heartbeat is older than the lease TTL counts
as dead.

## Leases and the leader

A **lease** is "exactly one instance does this". Leases live in `cluster_leases`; acquire and
renew are one atomic `UPDATE … WHERE holder = me OR expires_at < now`, compared on the
**database clock** so instance clock drift cannot steal or extend a lease. A holder renews every
heartbeat; if it cannot reach the coordinator for a full TTL it treats the lease as lost and stops
the work (fencing) — by then a peer may own it.

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

## Readiness and drain

`GET /api/ready` is the one contract every load balancer and Kubernetes readiness probe uses. It
is 200 only when boot finished, the database and Vault are connected, the instance is not
draining, the coordinator is up (cluster mode) and no hard preflight failure concerns this
instance (`coordinator`, `storage_secret`).

On SIGTERM with `LYNDRIX_DRAIN_SECONDS > 0`: `/api/ready` flips to 503, the instance waits for the
load balancer to notice, closes every SSE stream with an `event: reconnect`, releases its leases
and only then lets uvicorn shut down. A second SIGTERM forces an immediate exit.

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
