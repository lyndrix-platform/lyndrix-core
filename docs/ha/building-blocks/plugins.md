# Building block: plugin artifacts and placement

## Contract

- **Identical plugin artifacts on every instance.** Routes and React bundles are served by
  whichever instance answers; a plugin that exists on A only produces 404s on B.
- Background work runs **once**, not N times — via a lease or a pinned role.
- A plugin never needs local disk that only one host has, unless it is pinned to that host's role.

## How artifacts converge

`plugin_states` in the database is the source of truth. Installing, enabling, disabling or
uninstalling a plugin on one instance writes (or deletes) the row and broadcasts
`plugin:desired_changed`; every other instance reconciles: a folder that exists on disk but is
not loaded is loaded (shared `/app/plugins` mount), a missing plugin is installed from its
`repo_url` at the version the cluster runs (separate disks), enable/disable is applied without
re-persisting, and a plugin that is loaded here but has no row any more is uninstalled here too
(after a two-minute grace period for freshly loaded plugins). Reconcile installs are silent — only
the instance that executed the operator's action announces the install. A periodic pass every
five minutes catches missed events. The preflight `plugin_artifacts` check warns while the loaded
sets differ.

| Option | Notes | When |
|---|---|---|
| Shared `/app/plugins` mount | one install writes the files for everyone; the peers only load | simplest on Docker |
| Separate disks + reconcile from `repo_url` | each instance downloads the release tag itself | default behaviour, no shared mount needed |
| Plugins baked into the image | the install UI only records the wish; rebuild the image to apply | immutable deployments, Kubernetes |

## Placement

| `placement` | Use for |
|---|---|
| `any` | stateless routes and work you guard yourself with `ctx.on_lease` |
| `singleton` | a plugin whose entire background work must run once (every `on_lease` binds to one lease) |
| `pinned:<role>` | host-bound plugins (Docker socket, git working trees, local job logs) |

Shipped plugins:

| Plugin | Placement | Behaviour in a cluster |
|---|---|---|
| State Monitoring | `any` + lease `monitoring:prober` | every instance serves the API; exactly one probes; monitor changes are announced with `monitoring:monitors_changed` (`cluster=True`) |
| IaC Orchestrator | `pinned:runner` | runs on the `runner` instance only; others answer 503 remote; route its paths there |
| others | `any` | run on every instance; those with per-instance state (Docker Manager host list, Meeting Bingo games, Discord message map) are on the roadmap |

## Lyndrix settings

`LYNDRIX_INSTANCE_ROLES`, `LYNDRIX_PLUGINS_DESIRED`, `LYNDRIX_PLUGINS_AUTO_UPDATE`.

## Verify

Install a plugin on instance A (Settings → Plugins) and watch `GET /api/plugins` on B — it
appears within seconds (shared mount) or after the download. Settings → Cluster → *Plugin
placement* shows where every plugin runs and who holds its lease.

## Known limits

- No leader verification before N instances download the same release (each instance verifies
  its own install and auto-reverts on failure).
- `LYNDRIX_PLUGIN_ARTIFACTS=image` (report-only mode) is not implemented.
- Orchestrator jobs are not yet tagged with an owner instance; a runner restart resumes or fails
  its own jobs exactly as today.
