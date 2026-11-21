# Feature plan: High availability (HA) for Lyndrix Core + plugins

**Status:** 🚧 Phase 0 + 1 implemented (Core 1.4.0 / API 1.7.0, see implementation status in [`high-availability-implementation.md`](./high-availability-implementation.md)); phases 2–4 open
**Created:** 2026-10-14 from three parallel surveys (core state inventory, plugin inventory, actual deployment in `iac-environment`)
**Affected repos:** `lyndrix-core`, `lyndrix-ui`, all `lyndrix-plugin-*`, SSoT `aac-lyndrix-core` (+ new SSoTs for DB/Vault/VIP)
**Builds on:** [`rbac-security-k8s-audit.md`](./rbac-security-k8s-audit.md) part C (multi-pod readiness, July 2026) — the points A–J listed there are checked here, extended (plugins, data tier, secrets tier) and brought into an implementation path.

> Convention: prose German, code/identifiers/APIs English. All findings are backed by `file:line`
> (state core 1.3.0 / API 1.5.0, branch `feature/plugin-runtime-status`), so that implementation can proceed
> without new research. Paths in the core are relative to `lyndrix-core/app/`,
> in plugins relative to the plugin root, in the IaC workspace with the abbreviations
> **AR** = `aac-platform/aac-application-defenitions/applications-repository`,
> **CTL** = `iac-platform/iac-controller/environments`,
> **TE** = `aac-platform/aac-template-engine`.

---

## 0. Summary

Lyndrix today is **a stateful process** on **one VM** with **one** MariaDB,
**one** Vault (file storage) and bind mounts on local disk — and according to the backup audit this VM has **never been successfully backed up**. The availability of Lyndrix is therefore exactly
the availability of VM 8005 on `cerberus`.

The path to "N backends, N frontends, N DB nodes, N Vault nodes" has four levels that can be scaled independently, **if** the core gets three things it does not have today:

1. **A coordination layer** (instance registry, leader lease, TTL store, event fan-out) —
   DB-based, without a new mandatory dependency; Redis/NATS as optional driver.
2. **A placement semantics for plugins** (`any | singleton | pinned`), so that background
   work (probes, reconcilers, runners) runs exactly once instead of N times.
3. **Externalized state**: everything that today lives in process memory or local files
   (notifications, OIDC intermediate states, rate limiters, plugin artifacts, attachments) moves to
   the DB, a shared secret or an object store.

The data tier and the secrets tier are decoupled from this: **MariaDB Galera ×3 behind MaxScale with exactly
one writer** and **Vault/OpenBao with Raft ×3** work with today's data model,
as long as all write accesses run through one node (the model uses `SELECT … FOR UPDATE`,
`GET_LOCK` candidates and read-modify-write; multi-writer Galera would break this).

**Recommended order:** Phase 0 (backups, readiness, boot races — also makes today's
single instance robust) → Phase 1 (coordination layer in the core, API 1.6) → Phase 2 (adapt plugins) → Phase 3 (Galera, Raft, object store) → Phase 4 (deployment profile: Compose ×3 on
the three Olympus nodes **or** k3s). Details in §10, open decisions in §12.

---

## 1. Current state: how Lyndrix runs today

Source: `AR/aac-lyndrix-core/service.yml` (schema v2), host block `CTL/sites/onprem/stages/prod/hosts.yml:377` (service assignment `:504`), audit notes in `~/gitlab/iac-audit/`.

| Aspect | Today |
|---|---|
| Host | `docker-auto` 10.1.130.71 = Proxmox **VM 8005 on cerberus**, 13 GB RAM, not Terraform-managed (`is_managed: false`); shares the VM with Home Assistant, EMQX, Node-RED, n8n, Zigbee2MQTT |
| Stack | **one** Compose project: `aac-lyndrix-core` (ghcr 1.1.1, :8081), `aac-lyndrix-ui` (nginx, :8282, router `mngm.<domain>`), `aac-lyndrix-db` (MariaDB 12.2.2 from `TE/catalog/mariadb.yml`, single), `aac-lyndrix-vault` (`hashicorp/vault:2.0.3`, **file storage**, `tls_disable`, single) |
| Core container | no healthcheck, `restart: unless-stopped`, mounts `/var/run/docker.sock` (orchestrator runner + self-restart), `HOST_*` paths under `/export/docker/aac-lyndrix-core/…` → **host-bound** |
| Volumes | all bind mounts on the local VM disk: `plugins`→`/app/plugins`, `storage`→`/data/storage`, `secure_data`→`/data/security` (Vault keyfile!), `db`→`/var/lib/mysql`, `vault_data`→`/vault/file` |
| Ingress | per-host Traefik (`docker_standard` profile), router `lyndrix.int.fam-feser.de` on port 8081, IONOS cert |
| Backups | **none** in the SSoT (no `mariadb-dump`, no Vault snapshot); VM 8005 is in the vzdump job "AD - ONPREM", which has **never successfully backed it up** (`iac-audit/2026-09-25-backup-abdeckung.md`) |
| Leftovers | `docker-auto:/export/docker/aac-lyndrix-core-bak`, `docker-gpu:/export/docker/aac-lyndrix-core`, `docker-dmz:aac-lyndrix-showcase` |
| Docs | `aac-iac-documentation/content/aac-services/aac-lyndrix-core/documentation.md` generated, outdated (image 0.1.3, 2026-06-19) |

Which HA building blocks the fleet **has** and **does not have** (details §11):

| Building block | Available? | Where | HA-capable as deployed? |
|---|---|---|---|
| MariaDB | yes, sidecar per app | `TE/catalog/mariadb.yml` | no |
| Galera / MaxScale / ProxySQL / Patroni | **no** | — | — |
| Vault (Lyndrix) | yes, bundled | `aac-lyndrix-vault` | no (file, single) |
| OpenBao (central) | yes, 2.5.2 | `cicd-core` (Hetzner) via legacy role; SSoT `aac-hashivault` test only | no (file, manual unseal) |
| Redis/Valkey | only sidecars | various SSoTs | no |
| NATS / RabbitMQ / Kafka / etcd / Consul | **no** | — | — |
| MQTT (EMQX 5.8.8) | yes, single | `docker-auto` | no |
| MinIO (S3) | yes | `cicd-core` 10.100.1.5 (TF state) | single |
| TrueNAS (NFS/CIFS) | yes, onprem | olympus | single |
| k3s | yes: 3 masters + 2 workers | 10.100.1.100–121 (Hetzner network) | control plane yes |
| Longhorn / MetalLB (10.100.1.150–190) / cert-manager / Traefik v3 | yes | k3s | yes (within k3s) |
| GitOps for k8s | Rancher Fleet present, unused by the repo | k3s | — |
| VIP (keepalived / CARP / anycast) | **no** | — | — |
| Engine replicas / multi-host | only unused k8s templates (`TE/templates/kubernetes/`); Compose knows no `deploy:` | — | no |
| Lyndrix metrics | **no** (Prometheus only scrapes Traefik/cAdvisor) | `AR/aac-prometheus` | — |

---

## 2. Target picture: what "HA" means for Lyndrix

**Goals**

- No single point of failure in the **app tier**: failure of one core instance, one UI instance, one
  DB node or one Vault node is invisible to users (running SSE streams reconnect,
  running orchestrator jobs may visibly be "taken over").
- **Rolling restart without downtime** for core releases and plugin updates.
- **Surviving the failure of a hypervisor** — concretely motivated by hydra (OOM 09-25, kcompactd soft lockup 10-11, dead for two days).
- **RPO ≤ 1 h, RTO ≤ 15 min** for the data tier (today: RPO = ∞, because there is no backup).
- Each tier independently scalable: N frontends, N backends, N DB nodes, N Vault nodes.

**Non-goals (deliberate)**

- **No cross-site active/active.** Galera and Raft are synchronous; over the WireGuard link
  onprem↔Hetzner every commit becomes a WAN round trip. An HA cluster lives at **one** site;
  the second site is the backup/DR target.
- **No NiceGUI in HA.** The legacy UI is websocket- and process-bound; HA instances run
  `LYNDRIX_UI_ENGINE=react`. Plugins with a NiceGUI-only UI (aac-ssot-manager) are pinned or
  replaced.
- **No multi-active IaC orchestrator.** It needs the Docker socket and Git working trees; it
  runs as a pinned worker instance (exactly one), API instances forward triggers.

---

## 3. Target architecture (deployment-agnostic)

```
            DNS lyndrix.int ──► VIP (keepalived)   |   k3s: Traefik LB IP (MetalLB)
                                        │
                          ┌─────────────┴─────────────┐
                          │ Ingress / TLS / LB        │  Traefik, health = GET /api/ready
                          └──┬──────────┬──────────┬──┘
             ┌───────────────┘          │          └───────────────┐
      ┌──────┴──────┐           ┌──────┴──────┐            ┌──────┴──────┐
      │ lyndrix-ui  │   × N     │ lyndrix-ui  │            │ lyndrix-ui  │  nginx, stateless
      └──────┬──────┘           └──────┬──────┘            └──────┬──────┘
             └───────────────┬─────────┴──────────┬───────────────┘
                      ┌──────┴──────┐      ┌──────┴──────┐      ┌──────────────────┐
                      │ core api #1 │      │ core api #2 │  …   │ core worker      │ placement:
                      │ (leader)    │      │             │      │ (orchestrator,   │ pinned
                      └──────┬──────┘      └──────┬──────┘      │  docker.sock,    │
                             │                    │             │  git working tree│
                             │                    │             └────────┬─────────┘
               ┌─────────────┴────────────────────┴──────────────────────┴───────────┐
               │ Coordination (in the DB, Redis/NATS optional):                      │
               │ cluster_instances · cluster_leases · cluster_kv · cluster_events    │
               │ → leader lease, plugin leases, TTL store, bus bridge + SSE relay    │
               └─────────────┬────────────────────┬──────────────────────┬───────────┘
                      ┌──────┴──────┐      ┌──────┴──────┐        ┌──────┴──────┐
                      │ MaxScale    │      │ Vault/Bao   │        │ Object store│
                      │ (1 writer)  │      │ Raft × 3    │        │ S3 / NFS    │
                      └──────┬──────┘      └─────────────┘        └─────────────┘
                      ┌──────┴──────┐
                      │ Galera × 3  │
                      └─────────────┘
```

**Contract per component**

| Component | Scaling | State | Hard requirement |
|---|---|---|---|
| `lyndrix-ui` | N, arbitrary | none | `LYNDRIX_BACKEND_URL` → backend service/VIP; same origin |
| `core api` | N, arbitrary | only derived (registry, router, caches) | React-only; `/api/ready`; coordination via DB; identical plugin artifacts; same `STORAGE_SECRET` |
| `core` leader | exactly 1 (lease, automatic failover) | lease row | runs seeds/migration, reconciler, update checker, messaging outbox, `singleton` plugin jobs |
| `core worker` | exactly 1 per role (pinned) | Git tree, Docker socket, job logs | orchestrator; receives triggers via `cluster_events`; `owner_instance` on jobs |
| MariaDB | 3 Galera nodes, **1 writer** via MaxScale | everything persistent | every table has a PK (Galera); DDL only in the migration job; retry on 1213/1205/2006/2013 |
| Vault/OpenBao | 3 Raft nodes | secrets, settings | unseal of all nodes by the Lyndrix leader (or transit); keyfile as shared secret; non-root token |
| Object store | 1 (S3/MinIO or NFS), degrades gracefully | attachments, backgrounds, themes, plugin zips | `ctx.storage` abstraction with local/S3 driver |
| Coordination | in the DB (phase 1), Redis driver optional | leases, TTL KV, event log | poll ≤ 1 s; leases 30 s TTL / 10 s heartbeat |

---

## 4. Core findings: what breaks with 2+ instances

### 4.1 Three blockers that are easily overlooked

1. **Simultaneous first boot of two instances leaves one hanging forever.** `_seed_default_groups`
   (`core/components/auth/logic/auth_service.py:99-119`) has no error handling, `Group.name` is
   unique (`auth/logic/models.py:89`). The loser throws in `_bootstrap_iam`; `initialize_iam`
   only logs (`:86-87`) and never emits `iam:ready` → instance stays in `waiting_core`, no retry.
   The same window applies to `create_all` (`:91`), `_ensure_plugin_state_schema` (`plugins/logic/manager.py:979-1008`,
   unprotected `ALTER` → `_activate_saved_plugins` aborts, **no plugins start**) and the
   `PluginState` insert (`manager.py:564-647`, IntegrityError uncaught).
2. **`/api/health` reports `ok` during boot.** Middleware lets `/api` through during boot
   (`main.py:230-253`); with an empty registry `worst == "ok"` remains (`main.py:418-463`). There is no
   readiness/liveness split, DB/Vault/boot phase are not checked → a load balancer
   sends traffic to an instance that is not yet ready (or whose Vault is sealed).
3. **React SSO only works with NiceGUI enabled.** `/auth/callback/oidc` is only registered under
   `_nicegui_enabled` (`main.py:341-343`, `auth/ui/routes.py:104`). HA instances run
   React-only → SSO would be dead. In addition, the entire OIDC intermediate state lives in process memory
   (`auth/logic/providers/oidc.py:70-76`, `auth/ui/routes.py:17-18`, `core/api/auth_api.py:199-215`),
   and the flow spans three requests that must hit the same instance.

### 4.2 All findings, ranked

Column "July" = point from `rbac-security-k8s-audit.md` part C. ✅ = done since July.

| # | Area | Finding (`file:line`) | Breaks with N instances | July | Smallest fix |
|---|---|---|---|---|---|
| 1 | Event bus | `GlobalEventBus` purely in-memory, `emit()` only local (`core/bus.py:7-12, 97-129`); `ctx.request()` matches replies via process-local futures (`plugins/logic/context.py:80, 217-268`) | `plugin:files_changed`, `plugin:state_changed`, `plugin:runtime_status`, `ui:needs_refresh`, theme/settings changes, `system:maintenance_mode` reach only the emitting instance | A | local bus stays; **bridge** for an allowlist of broadcast topics via `cluster_events` with `origin` instance ID; `ctx.request` stays local |
| 2 | SSE | `_SSE_QUEUES` process-local (`core/api/events_api.py:38, 153-175`); `system:metrics_update` shows values of the holding instance | client on B gets nothing from A (installs, `notification:new`, runtime status) | B | feed `_publish` from the bridge (every instance relays all allowlisted topics into its streams) |
| ✅ | SSE ticket | HMAC from `STORAGE_SECRET` (`events_api.py:66-87`), deterministic, random only with empty secret | — | C | done; only the same `STORAGE_SECRET` everywhere |
| 3 | Boot races | §4.1 no. 1; additionally `_seed_or_update_user` read-modify-write (`auth_service.py:246-281`), role ledger JSON RMW (`role_registry.py:159-260`) | one instance hangs / plugins do not start | I | bootstrap + DDL + seeds under `GET_LOCK('lyndrix_bootstrap', 60)` **or** a one-time migration job (Alembic); seeds `INSERT … ON DUPLICATE KEY` / IntegrityError-tolerant |
| 4 | DDL everywhere | `create_all` + `ALTER` on **every** instance at **every** boot and reconnect: `auth_service.py:91, 144-172`, `manager.py:979-1008`, `notification_router/logic/router_service.py:78-110`, `custom_repo_service.py:46`, `preferences_service.py:60`, `plugin_service.py:215`, messaging `init_db` | races; on Galera every DDL is a TOI statement that halts the cluster | I | migration job; instances only `checkfirst` |
| 5 | DB reconnect | watchdog `SELECT 1`/15 s (`database/logic/db_service.py:137-150`); loop gives up **permanently** after 30×5 s → maintenance mode (`:94-130`); success **re-emits `db:connected`** (`:106`) → IAM bootstrap + `load_all()` run again (`auth_service.py:59`, `boot_service.py:42, 52`) | MaxScale failover (10–30 s) is ok, but a longer outage permanently kills the instance; re-bootstrap during operation | — | retry endlessly with backoff; reconnect emits `db:reconnected` without re-bootstrap |
| 6 | DB connection | one `DB_HOST` (`config.py:47, 186-187`), default pool 5+10, no `pool_recycle` (`db_service.py:79-83`); `last_used_at` written on **every** request (`api_key_service.py:142-150`, 1020 conflicts already seen) | hot row → certification conflicts on Galera | — | `DB_HOST` → MaxScale; `pool_recycle`; `last_used_at` only if older than 60 s |
| 7 | Vault unseal | state machine runs once on `system:started`, retry only on exception (`vault/logic/vault_service.py:66-114`); unseal via `/api/vault/unseal` on A leaves B in `needs_unseal`; auto-unseal needs `LYNDRIX_MASTER_KEY` **and** local keyfile `/data/security/vault_keys.enc` (`auto_unseal.py:24-34`, `vault_init.py:65-84`); `is_connected` is **never** set to false again, `read_secret_dict` returns `{}` on error (`kv_helper.py:157-179`) | only one Vault node is unsealed; instances without keyfile hang; sealed Vault → settings read silently as empty | F | leader polls `sys/health` of **all** `VAULT_NODES` and unseals each; keyfile as shared secret; emit `vault:status_changed` (is allowlisted, never sent); non-root token (AppRole) |
| 8 | Settings drift | `hydrate_from_vault()` once on `vault:opened` (`config.py:351-391`); `POST /api/system/config` → Vault RMW without CAS + `setattr` **locally** (`settings/logic/system_config_service.py:77-91`); `ctx.set_secret` locked only process-locally (`context.py:498-529`) | other instances keep old `DEFAULT_THEME_ID`, `LOG_LEVEL`, auth settings until restart | — | `settings:changed` via bridge → peers `hydrate_from_vault`; KV v2 `cas` |
| 9 | Plugin artifacts | code in `/app/plugins` (`plugin_service.py:57`, `manager.py:31, 89`); install = zip → tempdir → swap + `pip --target vendor` (`plugin_service.py:686-905`); `plugin:files_changed` loads only locally (`manager.py:271-339`); `toggle_module` only locally (`manager.py:651-719`) | A installs X, B has no files → 404 on router and bundle; `_activate_saved_plugins` only activates what is on disk; reconcile only knows `LYNDRIX_PLUGINS_DESIRED`, not marketplace installs | F, G | plugin code = **immutable artifact**: bake into the image **or** every instance reconciles `/app/plugins` from `plugin_states` (desired version) on `plugin:desired_changed` + periodically |
| 10 | Core background jobs | collection watcher 900 s (`plugin_service.py:295-328`), update checker (`:342-420`, dedupe `_latest_known` process-local → N notifications), messaging expiry 300/600 s (`messaging/entrypoint.py:79-90`), gateway retry queues in memory (`messaging/logic/gateway.py:54-120`) | N-fold; retry queue is lost on restart | G | leader lease; retry → DB outbox |
| 11 | Notifications | `deque(maxlen=500)` + `/data/storage/notifications.json`, loaded once on import (`notifications/logic/notification_service.py:37-104`) | each instance has its own history/read status; shared file = last-writer-wins | — | DB tables + `notification:new` via bridge |
| 12 | Messaging correlation | `messaging_pending_actions` in DB, but `resolve()/get()` read **only** the cache (`messaging/logic/correlation.py:141-172`); `StreamBridge` in memory | Discord button callback lands on B → "correlation_id not found", action lost | — | DB fallback on cache miss, atomic `UPDATE … WHERE resolved_at IS NULL` |
| 13 | Rate limiter | login 5/60 s in memory (`auth_api.py:25`, `vault/logic/rate_limit.py:97-134`), key `username|client.host`; uvicorn **without** `--forwarded-allow-ips` (`docker/Dockerfile:67`) → behind Traefik `client.host` is the LB IP | limit ×N; **already today**: one bucket per username for all clients → trivial lockout | E | `--forwarded-allow-ips` + `X-Forwarded-For`; counter in `cluster_kv` |
| 14 | OIDC/SSO | §4.1 no. 3 | callback on A, token exchange on B → "invalid code" | D | callback out of the NiceGUI gate; state/PKCE/codes in `cluster_kv` with TTL |
| 15 | Self-restart | `POST /api/system/restart` → `docker restart <own>` via local socket (`main.py:556-595`, `sockets/providers/docker_provider.py:17-33, 82-91`) | starts the randomly answering instance; needs socket | H | HA mode: `cluster_events` "restart requested" → every instance exits gracefully (exit 0, restart policy) **or** orchestrator rollout |
| 16 | Local disk | `/data/security/vault_keys.enc`; `/app/plugins`; `notifications.json`; `/data/storage/user_backgrounds` (`auth_api.py:49-50, 629`); theme uploads in `app/assets/themes/` **in the image layer** (`themes_api.py:18, 142-193`); `/app/logs/lyndrix.log` RotatingFileHandler (`core/logger.py:122-135`); `/app/.nicegui/storage-user-*.json`; `/data/storage/git_repos` (`git/logic/git_service.py:46`) | divergent per instance; theme upload lost on redeploy; N writers corrupt one log file | F | keyfile = secret; backgrounds/themes/attachments → object store; logs → stdout; Git only on worker |
| 17 | NiceGUI | `ui.run_with(storage_secret=…)` (`main.py:645`), storage local, websocket per client; NiceGUI-only: `/`, `/login`, `/auth/complete`, `/dashboard`, `/settings`, `/plugins`, `/setup`, `/unseal` + plugin pages | without sticky session every page breaks; login on A unknown on B | J | HA = `LYNDRIX_UI_ENGINE=react`; REST equivalents exist for everything except the OIDC callback (#14) |
| 18 | Health | one endpoint, calls `health()` of **every** active plugin on **every** request (`main.py:399-470`, 5 s timeout per plugin) | LB probe × dashboards × N = load; no boot/DB/Vault check | — | `/api/live`, `/api/ready` (boot phase ∧ DB ∧ Vault, without plugins), `/api/health` deep + cached 10 s |
| 19 | Caches/invalidation | permission cache 5 s (`access_service.py:33-34`), system key 30 s (`core/api/security.py:56-57`), provider registry reinit local (`auth_service.py:208-217`), i18n version from `mtime` (`core/i18n.py:130-153`) → 200/304 flicker | bounded staleness; cache churn | — | invalidation events via bridge; i18n hash over content instead of mtime |
| 20 | Runtime status (API 1.5) | per instance (`context.py:83`, `router_registry.py:58-88`) | maintenance on A → no 503 on B; warm-up panel flickers depending on which instance answers | — | 503 gate stays local (correct); broadcast status with `instance_id`, shell aggregates |

**What is already HA-capable:** REST sessions are DB tokens (`UserApiKey`, SHA-256, no JWT,
no expiry) → React UI + REST are instance-agnostic; SSE tickets (✅ C); `ctx.get_setting`
reads Vault on every call; `max_statement_time` per connection; `plugin_states` in the DB.

---

## 5. Plugin findings

In general, for all: the bus is process-local, `ctx.request()` only works because
requester and responder sit in the same process (this remains so — responder plugins run on
every instance). **As soon as the bus is bridged, every side-effecting subscriber runs
N times** → the bridge may only transport an allowlist; plugin topics stay local unless
the plugin explicitly emits `cluster=True`.

| Plugin | Rating | Core finding (`file:line`) | Smallest fix |
|---|---|---|---|
| **monitoring** | 🔴 BLOCKER | scheduler per monitor per instance (`app/controller/service.py:47`), semaphore/inflight/circuit breaker process-local (`:54, 58, 81-83`); every probe N times; `_bump_daily_aggregate` RMW without lock (`:509-536`); **no UNIQUE(monitor_id, day)** (`app/model/models.py:73-80`) → duplicate rows; `monitoring:state_changed` N times (`:835-848`); inventory ingest resyncs only the local scheduler (`:469-486`); 503 gate per instance | prober behind `ctx.lease("monitoring:prober")` (later sharding `hash(monitor_id) % N`); `UNIQUE(monitor_id, day)` + atomic `UPDATE … SET up_samples = up_samples + 1`; `monitoring:monitors_changed` as cluster event |
| **iac-orchestrator** | 🔴 BLOCKER (pin) | in-memory `state`/dispatch locks/semaphore(1) (`app/controller/service.py:25-31`, `engine.py:905-922`); dispatch guard = set + `RUNNING` check, then `create_job` → TOCTOU (`engine.py:1355-1445`); **startup reconciliation** resumes or FAILs RUNNING jobs of **other** instances (`service.py:103-136`, `engine.py:1873`); Docker CLI `logs -f/wait/rm/run/login` local, fixed container names `aac-runner-*`/`iac-tf-*` (`engine.py:1946-2032, 2875-2925`); Git trees `/data/storage/git_repos/*` with in-process lock; job logs local; SSE `stream/jobs` from `active_tasks`; webhooks `POST /api/iac/webhook/*` public (`entrypoint.py:237`), hook URL from Vault setting | `placement: pinned:runner`; `owner_instance` on `iac_orchestrator_jobs`, reconciliation only for own jobs; triggers from API instances via cluster event; dispatch reservation as DB row; long term runners as k8s jobs |
| **aac-ssot-manager** | 🔴 BLOCKER | NiceGUI-only, process-global edit buffer `state` (`entrypoint.py:42-55`), writes directly to `/data/storage/git_repos/iac_env_main` (`:57, 72-101`); registers `/iac` — **collision** with the orchestrator | replace (React editor in the orchestrator) or pin to the worker; buffer per session |
| **meeting-bingo** | 🔴 BLOCKER (feature) | games/boards only in the process (`app/logic/service.py:57-62`); scoreboard as Vault blob LWW (`:91-130`) | sessions in a DB table |
| **docker-manager** | 🟡 NEEDS-WORK | host list = Vault blob, loaded once, every mutation writes the whole list (`app/logic/service.py:51-52, 123-128`), IDs `max+1` local (`:199`) | DB table with UNIQUE(name), or Vault on every read + CAS |
| **server-manager** | 🟡 NEEDS-WORK | `ALTER … ADD COLUMN` **without** `IF NOT EXISTS` (`app/model/database.py:25-70`) → loser stays `_ready=False` until the next `db:connected`; catalog path not persisted (`service.py:100-104`) | `IF NOT EXISTS`; path in settings |
| **entity-registry** | 🟡 NEEDS-WORK | `_create` loses race → `RegistryError` without retry (`app/logic/service.py:406-429`) → hosts without `ci_id` until the next publish; `registry_conflicts.fingerprint` not unique (`models.py:128`, `:462-495` check-then-insert) | retry via re-match; `UNIQUE(fingerprint)` + catch IntegrityError |
| **itsm** | 🟡 NEEDS-WORK | attachments on local disk `/data/storage/itsm/attachments` (`service.py:86`); number assignment `SELECT … FOR UPDATE` + retry (`:121-131, 285-294`) **cluster-safe with single writer**; `version` checks present | `attachment_dir` → object store |
| **discord-notifier** | 🟡 NEEDS-WORK | `_message_store` notification_id→message_id in memory (`app/logic/adapter.py:137`) → live edits become new posts; 429 handling per instance (`:287, 391`); interactions endpoint needs stable URL | store in DB |
| **external-services** | 🟡 NEEDS-WORK | sidebar in core registry only local (`routing.py:66-93`); **`health()` calls `ensure_table()` = DDL on every poll** (`service.py:52-82`) | sidebar from DB per request; DDL out of `health()` |
| collection / plugin-ui-template | ⚪ | no runtime code | — |

**Background jobs across all plugins** (N-fold without lease):

| Plugin | Job | Interval | Duplicate-safe? | Guard |
|---|---|---|---|---|
| monitoring | probe per monitor | `interval_seconds` | **no** (N probes, lost updates, N `state_changed`) | lease or sharding + atomic counters |
| monitoring | startup sweep | per boot/ingest | no | lease |
| monitoring | prune / rollups / uptime windows | hourly, daily | yes, but N× load | lease (optional) |
| iac-orchestrator | startup reconciliation / `resume_bulk_rollout` | per boot | **no** (double rollouts or foreign jobs FAILed) | `owner_instance` / pinned |
| iac-orchestrator | pipeline per `iac:webhook_verified` | per event | **no** (TOCTOU, semaphore local) | DB reservation / pinned |
| iac-orchestrator | webhook sync loop | 1800 s | yes (N× GitLab API) | lease |
| iac-orchestrator | inventory publish (`emit_iac_inventory_updated`, monitoring sync) | per boot | mostly (registry insert race, Vault RMW in docker-manager) | leader-only |
| docker-manager | `sync_orchestrator_hosts` | per publish | idempotent, Vault RMW races | DB table |
| others | none | — | — | — |

**Host-bound resources:** orchestrator (Docker daemon via socket manager + CLI; Git trees;
`.iac_keys`, `secrets.json`; job logs; `terraform-providers`) → **pin**; aac-ssot-manager
(same Git tree) → pin to the worker; ITSM attachments → shared/object store **mandatory**;
catalogs (server-manager/itsm/registry YAML) → identical artifacts; monitoring needs `NET_RAW`
per instance, probe source IP varies (firewall ACLs!); docker-manager/discord only outbound.

**Stable public addresses required:** `/api/iac/webhook/{gitlab,sync,gitlab/test-host/*}`,
Discord `/api/plugins/lyndrix.plugin.discord/interactions`, monitoring `/passive` + `/inventory-sync`,
server-manager `/servers/external/{source}/{id}` → all via the VIP; the Vault setting
`iac_lyndrix_base_url` points to the VIP.

---

## 6. Data tier: N databases

**Decision: Galera ×3 with exactly one writer (MaxScale `readwritesplit` with
`master_accept_reads=true`, initially all reads on the writer).** Rationale:

- The Lyndrix data model assumes single writer: `SELECT … FOR UPDATE` (ITSM counter),
  `GET_LOCK` (planned for bootstrap), read-modify-write on JSON columns (group permissions,
  role ledger). On Galera row locks are **not cluster-wide** (conflict only at commit →
  deadlock 1213), `GET_LOCK` only works locally. All sessions on one node = semantics as today.
- Galera provides synchronous replication without lag and automatic failover (MaxScale selects a new
  writer within seconds) — primary/replica with asynchronous replication would lose data on failover.
- Alternative primary + semi-sync replica + MaxScale `mariadbmon` (auto_failover) is simpler,
  but a replica rejoin is manual work; with three nodes Galera pays off.

**Requirements for the app (core work package):**

| Requirement | Why | Where |
|---|---|---|
| Every table has a PK | Galera requirement (otherwise replication errors) | met today (check for new plugin tables, lint in `check_plugin_*`) |
| DDL only in the migration job, never in `setup()`/`health()` | TOI DDL halts the cluster; external-services does DDL on every health poll | §4 #4, §5 |
| Retry wrapper for 1213 (deadlock), 1205 (lock wait), 2006/2013 (connection lost on failover) | failover visible as aborted transaction | `db_service` `run_with_retry(fn, attempts=3)`; keep ORM sessions short |
| throttle `last_used_at` | hot-row writes = certification conflicts | `api_key_service.py:142-150` |
| `pool_recycle=300`, `pool_pre_ping` (present) | MaxScale/Galera disconnect idle connections | `db_service.py:79-83` |
| `DB_HOST` = MaxScale endpoint (or k8s service), `DB_HOSTS` optional for fallback list | one endpoint, failover in the proxy | `config.py` |
| batch jobs in chunks (present: prune 20 k) | large transactions = large write sets (`wsrep_max_ws_size`) | monitoring |
| `wsrep_sync_wait=1` **only** if reads later go to other nodes | read-your-writes | MaxScale config |

**Deployment options:** Compose: extend `TE/catalog/mariadb.yml` with a Galera variant
(`mariadb:12` with `wsrep_*`, ports 4567/4568/4444, `galera_new_cluster` only at bootstrap) as a
new SSoT `aac-lyndrix-db` **per host**, plus `aac-maxscale` (one MaxScale per host, addressed locally →
no additional SPOF). k3s: `mariadb-operator` (CRD `MariaDB` with `galera:` +
`MaxScale` CRD) on Longhorn — a ready-made solution incl. backups (`Backup` CRD) and rolling updates.
**Migration:** `mariadb-dump --single-transaction` from `aac-lyndrix-db` → import into Galera node 1
→ nodes 2/3 join via SST.

**Backups (phase 0, independent of HA):** nightly `mariadb-dump` (or `mariabackup`) into the
object store/TrueNAS + Vault snapshot (`vault operator raft snapshot save` or file-storage tar
with Vault stopped) + documented restore exercise. vzdump remains the second level.

---

## 7. Secrets tier: N Vaults

**Decision: Raft integrated storage ×3, unseal responsibility stays with Lyndrix.**

- Vault Raft requires every node unsealed; standbys forward API requests to the active node.
  A simple LB across all three nodes is sufficient if all are unsealed (health check:
  `sys/health` returns 200 active / 429 standby / 503 sealed → LB policy "200 or 429 = healthy").
- **Core change:** `VAULT_NODES` (list) next to `VAULT_URL`; the **leader** polls `sys/health`
  of every node (10 s), unseals sealed nodes with the key from the keyfile, emits
  `vault:status_changed` (topic is allowlisted, never sent today) and sets
  `is_connected`/`ui_state`/maintenance mode correctly (§4 #7). The init state machine accepts
  `VaultAlreadyInitializedError` as "a peer was faster".
- **Keyfile:** `/data/security/vault_keys.enc` is encrypted with Argon2+AES-GCM using `LYNDRIX_MASTER_KEY`
  → can live as a k8s secret / shared secret alongside the master key without lowering the
  security level (both together = today both on one VM as well). Alternative
  **transit auto-unseal** against the central OpenBao on `cicd-core` — would make onprem Lyndrix depend on the WAN → only once OpenBao itself becomes HA.
- **Token:** today root token from the keyfile (`vault_service.py:47-64`). HA package: AppRole with
  `role_id`/`secret_id` from env/secret, periodic token, renewal loop. Not a mandatory part of HA,
  but the same code path — take it along.
- **Vault vs. OpenBao:** `hashicorp/vault:2.0.3` is BSL-licensed; OpenBao (in the fleet as
  "aac-hashivault" 2.5.2) is API-compatible with `hvac` and has Raft. Recommendation: **evaluate** OpenBao Raft
  in the Lyndrix stack (`vault operator migrate` file→raft works in both).
- **Migration:** `operator migrate -config` (file → raft) on node 1, then nodes 2/3 join;
  keyfile remains valid (same unseal key).

---

## 8. Frontend tier: N frontends

`lyndrix-ui` is nginx + static files + reverse proxy to `LYNDRIX_BACKEND_URL` (same
origin, no CORS). **Already scalable at will today.** Requirements:

- `LYNDRIX_BACKEND_URL` → backend VIP/service, not the container name of one instance.
- Ingress health on `/` (nginx) is sufficient; the ingress checks backend readiness separately.
- SSE through nginx: `proxy_buffering off`, `proxy_read_timeout 3600s` — **present** in
  `lyndrix-ui/docker/lyndrix-proxy.conf:19-21`.
- The Android app (TWA) points to a hostname → VIP.
- Plugin React bundles come from the backend (`/api/plugins/<id>/static/`) → identical
  plugin artifacts on all backend instances (§4 #9), otherwise 404 flicker.

---

## 9. Backend tier: N backends — the coordination layer (core API 1.6)

### 9.1 Why DB-based first, not Redis

- The basic rule of Marvin's "no plugin may *need* another plugin" applies analogously to the core: HA may not
  introduce a **new mandatory dependency** that itself first has to be made HA (Redis
  Sentinel/Cluster = another cluster that does not exist in the fleet).
- The DB is there in HA anyway and is highly available. Lyndrix' event rate is small (runtime status
  coalesces 1/s, notifications, state changes) → polling at 500 ms–1 s is unproblematic.
- The layer is an **abstraction** (`ClusterCoordinator`) with driver `db` (phase 1) and
  optionally `redis` (later, for sub-100 ms fan-out and NiceGUI storage).

### 9.2 Tables (driver `db`)

```sql
cluster_instances(instance_id PK, hostname, role, core_version, started_at, heartbeat_at)
cluster_leases   (name PK, holder, acquired_at, expires_at)
   -- acquisition/renewal atomic: UPDATE … SET holder=:me, expires_at=NOW()+30s
   --   WHERE name=:n AND (holder=:me OR expires_at < NOW()); heartbeat every 10 s
cluster_kv       (k PK, v JSON, expires_at)           -- OIDC state, SSO codes, limiter counters
cluster_events   (id BIGINT AUTO_INCREMENT PK, topic, payload JSON, origin, created_at)
   -- every instance polls WHERE id > :last_seen (≤ 1 s), ignores origin == self; prune > 1 h
```

Leader = holder of `cluster_leases['core:leader']`. Loss of the lease (instance dead) → after ≤ 30 s
another takes over; all leader tasks must be **idempotent and resumable**.

### 9.3 Plugin API extension

```python
manifest = ModuleManifest(
    ...,
    placement="any",            # any | singleton | pinned:<role>   (default: any)
)

def setup(ctx):
    ctx.register_routes(router)                       # runs on every instance (reads/writes via DB)
    ctx.on_lease("monitoring:prober",                 # exactly one instance holds the lease
                 acquired=service.start_probing, lost=service.stop_probing)
    ctx.emit("monitoring:monitors_changed", {...}, cluster=True)   # reaches all instances
    if ctx.is_leader: ...                             # core leader (for one-time tasks)

async with ctx.lease("itsm:connector_poll") as held:  # one-time work, held=False → skip
    ...
ctx.instance_id, ctx.cluster_size                     # diagnostics, metric labels
```

- `placement: singleton` → the core calls `setup()` on every instance (routes!), but the lease
  `plugin:<id>:singleton` is held automatically; `ctx.on_lease` callbacks follow it.
- `placement: pinned:<role>` → `setup()` runs only on instances with `LYNDRIX_INSTANCE_ROLES`
  ⊇ `{role}`; API instances answer for this plugin with 503 `X-Plugin-Runtime: {"phase":
  "remote"}` **or** proxy (decision §12). Triggers to the worker go via
  `ctx.emit(..., cluster=True)`.
- Old cores: plugins guard with `getattr(ctx, "on_lease", None)` as with `set_status`.

### 9.4 Core rework (work package phase 1)

| Package | Content | Files |
|---|---|---|
| Coordinator | tables §9.2, instance heartbeat (10 s), leader lease, `lease()`/`on_lease()`, `kv_set/get` with TTL, `publish()`/poll loop | new `core/components/cluster/logic/coordinator.py`, `core/api/__init__.py` export |
| Bus bridge | `event_bus.emit(topic, payload, cluster=True)`; allowlist of core topics: `plugin:state_changed`, `plugin:runtime_status`, `plugin:desired_changed`, `ui:needs_refresh`, `notification:new`, `settings:changed`, `auth:providers_changed`, `theme:changed`, `system:maintenance_mode`, `system:restart_requested`; incoming events are re-emitted locally with `payload["_origin"]` | `core/bus.py`, coordinator |
| SSE relay | `_publish` also from bridge events; `system:metrics_update` with `instance_id` | `core/api/events_api.py:153-175` |
| Readiness | `/api/live`, `/api/ready` (boot phase ∧ `db.is_connected` ∧ `vault.is_connected`, no plugins), `/api/health` 10 s cache; expose boot phase | `main.py:399-470`, `boot_service.py` |
| Bootstrap | `GET_LOCK('lyndrix_bootstrap')` around `create_all`/seeds/`ALTER`; seeds idempotent; **or** `scripts/migrate.py` as init job (Alembic) and instances only `checkfirst` | `auth_service.py:86-172`, `manager.py:564-647, 979-1008`, `router_service.py:78-110` |
| DB resilience | reconnect endlessly with backoff; `db:reconnected` instead of re-bootstrap; `pool_recycle`; `run_with_retry`; `last_used_at` throttle | `db_service.py`, `boot_service.py:42-52`, `api_key_service.py:142-150` |
| Vault HA | `VAULT_NODES`, health poll + multi-node unseal on the leader, `vault:status_changed`, keyfile from secret path, AppRole optional | `vault/logic/*`, `config.py:195-196` |
| Settings propagation | `settings:changed` → `hydrate_from_vault()` on peers; KV v2 `cas` in `system_config_service` and `ctx.set_secret` | `settings/logic/system_config_service.py:77-91`, `context.py:498-529` |
| Auth state | OIDC state/PKCE/handoffs/SSO codes → `cluster_kv`; login/unseal limiter → `cluster_kv`; `--forwarded-allow-ips`; OIDC callback out of the NiceGUI gate | `auth/logic/providers/oidc.py:70-76`, `auth_api.py:25, 199-215`, `main.py:341-343`, `docker/Dockerfile:67` |
| Notifications/messaging | notifications + read status in DB tables; correlation DB fallback + atomic resolve; gateway retry → `messaging_outbox`, processed by the leader | `notification_service.py`, `correlation.py:141-172`, `gateway.py:54-120` |
| Plugin artifacts | reconciler per instance: `plugin_states` (desired_version) → local download/install; marketplace install writes only DB + `plugin:desired_changed`; leader does collection sync/update check | `plugin_service.py`, `manager.py:921-977` |
| Restart | HA mode (`LYNDRIX_CLUSTER=1`): `/api/system/restart` → `system:restart_requested` → ordered shutdown per instance, staggered (leader last); no Docker socket needed | `main.py:556-595` |
| Placement | `placement` in the manifest, `LYNDRIX_INSTANCE_ROLES`, `pinned` gate in the router registry | `plugins/logic/models.py`, `router_registry.py` |
| Misc | logs → stdout (file optional), i18n version from content hash, `/metrics` (Prometheus, label `instance`), theme uploads/backgrounds → `ctx.storage` (local/S3) | `core/logger.py`, `core/i18n.py:130-153`, `themes_api.py`, `auth_api.py:629` |

Versions: `__api_version__` 1.6.0, `app/version.py` 1.4.0. Tests: `tests/cluster/` (lease acquisition,
loss, fan-out, KV TTL) on SQLite + a **two-instance dev compose**
(`lyndrix-core-dev` + `lyndrix-core-dev-2`, shared DB/Vault, `docker/traefik.dev.yml` already exists
as LB) as mandatory test bench for every HA change.

---

## 10. Implementation plan

| Phase | Goal | Packages | Repos | Effort |
|---|---|---|---|---|
| **0 — Survive** (immediately, sensible even without HA) | today's single instance robust and recoverable | backups (`mariadb-dump`, Vault snapshot, restore exercise); `/api/live` + `/api/ready` + Compose healthcheck + Traefik health; `--forwarded-allow-ips` (rate-limiter bug today); boot races idempotent + `GET_LOCK`; DB loop never gives up, no re-bootstrap; Vault health poll + `vault:status_changed`; OIDC callback out of the NiceGUI gate; notifications → DB; logs stdout | core, SSoT `aac-lyndrix-core` | M |
| **1 — Core cluster-capable** | two API instances behind the dev Traefik behave like one | §9.4 complete, two-instance dev compose, shell: aggregate runtime status with `instance_id` | core, ui | L |
| **2 — Plugins** | no plugin runs N-fold, none needs local disk except the worker | monitoring (lease prober, UNIQUE + atomic counters, cluster resync); orchestrator (`pinned:runner`, `owner_instance`, DB reservation, triggers via cluster event, job logs in DB/shared); ssot-manager (replace/pin); docker-manager (DB table); server-manager/registry/itsm (`IF NOT EXISTS`, retry, `UNIQUE(fingerprint)`); itsm attachments → `ctx.storage`; discord store → DB; external-services (sidebar from DB, no DDL in `health()`); bingo (DB) | all plugins | L (orchestrator alone M–L) |
| **3 — Data & secrets HA** | Galera ×3 + MaxScale, Vault/OpenBao Raft ×3, object store | new SSoTs `aac-lyndrix-db` (Galera per host), `aac-maxscale`, `aac-lyndrix-vault` (Raft per host); core `VAULT_NODES`/`DB_HOST`→MaxScale; migration dump→Galera, `operator migrate` file→raft; switch backups | iac-environment, core | M–L |
| **4 — Deployment profile** | N instances in production, rolling update, chaos tests | profile A or B (§11); keepalived VIP (A); rollout order (migration job → worker → API instances one by one); tests: kill instance, kill DB node, seal Vault node, stop hypervisor; Prometheus scrape + Grafana dashboard | iac-environment | M |

Critical path: 0 → 1 → 2 (monitoring + orchestrator) → 3 → 4. Phase 3 can be prepared in parallel
with 2 (infra), but needs phase 1 (retry/reconnect/`VAULT_NODES`) before switching over.

**Verification per phase**

- 0: `docker stop lyndrix-db` → core stays alive, `/api/ready` 503, after `start` back to 200
  without re-bootstrap; restore exercise from dump on `docker-test` documented.
- 1: two instances, plugin install on A visible on B (SSE + router + bundle); theme change
  propagates ≤ 2 s; leader kill → new lease ≤ 30 s; `/api/health` consistent; OIDC login with
  forced instance change between callback and exchange.
- 2: heartbeat rate with 2 instances = with 1; `monitoring_daily_aggregates` without duplicates;
  orchestrator job continues running if an API instance dies; API instance restart fails no job.
- 3: `kill -9` on Galera writer → MaxScale selects new writer, core requests ≤ 1 retry; Vault
  node `seal` → leader unseals ≤ 20 s; `sys/health` per node in `/api/health`.
- 4: stop hypervisor hydra → `lyndrix.int` keeps responding; rolling update without 5xx at the LB.

---

## 11. Deployment profiles

**Site pre-decision:** Galera/Raft run at **one** site. Onprem Olympus has had **three** PVE
nodes since 2026-10-12 (hydra, cerberus, chimera) → real 3-fold placement; Hetzner titan
has two (prox1, cronus) → 3 nodes mean two on one hypervisor (loss of quorum on its failure; this
also applies today to the k3s masters). Lyndrix primarily manages the onprem infrastructure
(orchestrator → Proxmox, Docker hosts); if it runs in Hetzner, onprem administration depends on the WAN link.
→ **Recommendation: onprem Olympus.**

### Profile A — Docker Compose × 3 nodes (recommended first step)

- Three Docker VMs/LXCs, one per PVE node: `docker-cerberus` (.61), `docker-hydra` (.62), new
  `docker-chimera`. Chimera is a laptop → possibly only Galera arbiter (`garbd`) + Vault node +
  one API instance, no worker.
- SSoT rework: `aac-lyndrix-core` loses the `dependencies:` DB/Vault; instead own SSoTs
  `aac-lyndrix-db` (Galera node, one entry per host — the engine allows the same SSoT on
  several hosts as independent stacks, which fits exactly), `aac-maxscale` (per host, local),
  `aac-lyndrix-vault` (Raft node per host). `aac-lyndrix-core` on all three hosts with
  `LYNDRIX_INSTANCE_ROLES=api` (+ `runner` on today's `docker-auto` successor, which keeps the
  Docker socket and `/export/docker/...` paths).
- **VIP:** new role/SSoT `keepalived` (VRRP) on the three hosts, `lyndrix.int` → VIP; the
  per-host Traefik on the VIP holder terminates TLS and balances to the three core instances
  (Traefik file provider with health `/api/ready`). Alternative without new technology: PowerDNS
  round robin — browser failover is slow, only a transitional solution.
- Fits the existing pipeline (`cd_rollout` is host-scoped → rolling update = host by host).
- Limits: no scheduler, placement is manual work in `hosts.yml`; Compose knows no
  readiness-driven traffic control → Traefik health must provide that.

### Profile B — k3s

- Present: 3 masters, Longhorn, MetalLB, Traefik, cert-manager, Fleet. Lyndrix objects:
  `Deployment lyndrix-core` (replicas 2–3, `readinessProbe /api/ready`, `livenessProbe /api/live`,
  `LYNDRIX_UI_ENGINE=react`, plugins in the image or init-container reconcile), `Deployment
  lyndrix-ui`, `Job lyndrix-migrate` (pre-upgrade), `mariadb-operator` (Galera + MaxScale, backups
  as CRD), Vault/OpenBao Helm chart (Raft ×3, Longhorn), worker as a separate `Deployment` with
  `replicas: 1` + `nodeSelector` on a node with Docker (or DinD) — until runners become k8s
  jobs (redesign of the orchestrator, not part of this plan).
- Gain: rolling updates, readiness routing, operator-backed DB, GitOps with Fleet.
- Hurdles: cluster sits in Hetzner (site question above); `TE/templates/kubernetes/` is
  untested; orchestrator runners need Docker; no Lyndrix workload on k3s today.
- Makes sense as **target picture after profile A**, or immediately if an onprem k3s emerges
  (`aac-k3s-init` as template).

---

## 12. Open decisions

1. **Site** of the HA cluster: onprem Olympus (recommended) or Hetzner k3s?
2. **Profile A (Compose ×3 + keepalived) first** or straight to k3s?
3. **Coordination driver:** DB-only (recommended for phase 1) — Redis later only if needed
   (latency, NiceGUI storage)?
4. **Vault or OpenBao** in the Lyndrix stack (license, fleet consistency)? Transit auto-unseal against the
   central OpenBao or unseal by the Lyndrix leader (recommended, no WAN dependency)?
5. **`pinned` semantics** for API instances: 503 "remote" (simple) or reverse proxy to the worker
   (seamless, more code)?
6. **End of NiceGUI** in HA operation confirmed → replace aac-ssot-manager instead of pinning?
7. **Object store:** MinIO (new onprem or existing on `cicd-core`) vs. TrueNAS NFS volume?
8. **Migrations:** `GET_LOCK` around today's `create_all`/`ALTER` path (small) or Alembic +
   init job (clean, more work, mandatory for Galera TOI)?
9. **Monitoring prober:** one lease (one prober) is enough for ~160 monitors; sharding only at > 1 000?
10. **Chimera role:** full node (API + Galera + Vault) or arbiter only?

---

## 13. Sources

Three read-only surveys on 2026-10-14 (no files changed, no secrets output):
core state inventory (`lyndrix-core/app`, core 1.3.0), plugin inventory (10 plugins under
`lyndrix-dev/plugins/`), deployment inventory (`iac-environment`: `aac-lyndrix-core`,
`iac-controller`, `aac-template-engine`, `aac-k3s-init`, `iac-audit` reports of 2026-09-22/25).
Preliminary work: `features/rbac-security-k8s-audit.md` part C (2026-07-05).
