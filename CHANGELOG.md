# Changelog

All notable changes to lyndrix-core are documented in this file. Releases before 1.11.1 are
described by the auto-generated notes on
[GitHub Releases](https://github.com/lyndrix-platform/lyndrix-core/releases).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The core is released as the image `ghcr.io/lyndrix-platform/lyndrix-core:<version>`; the version is
`app/version.py` (a push to `main` builds the image and creates the tag `v<version>`).

## [1.12.0] - 2026-11-16
Result of the 2026-11 core audit (cluster, security, bugs, tests). Plugin API **1.14.0** (was 1.13.0).
Read **Upgrade notes** before rolling out — several defaults became stricter.

### Upgrade notes
- **Login sessions expire** 30 days after creation (`LYNDRIX_SESSION_MAX_AGE_HOURS`, default `720`, `0` =
  never; named API keys never expire). **Sessions that exist at the upgrade are grandfathered**: the
  first 1.12 instance records once, for the whole deployment, when the rule began to apply (table
  `auth_markers`), and sessions created before that instant do not expire by age. Nobody is signed out
  by the upgrade, a script that uses a `/api/auth/login` token keeps working, and nothing changes for
  the 1.11 instances during a rolling update — no key has to be created or renewed beforehand.
  `LYNDRIX_SESSION_MAX_AGE_LEGACY` chooses what the max-age means for those older sessions: `exempt`
  (default, never by age), `from_enforcement` (their age counts from the recorded instant — all of them
  end 30 days after the upgrade) or `strict` (like any other session; old ones end with their next
  request). Revoking a session, a password change and deleting the account end them as always. Set the
  same value on every instance. `GET /api/users/{u}/api-keys` shows `is_session`, `expires_at` and
  `expiry` per row. Long-lived automation should still move to a named API key at its own pace.
- **Vault and readiness**: a Vault outage *after the boot* (restart of the Vault pod, re-seal, network)
  no longer fails `/api/ready`. It is reported instead — `/api/ready` `degraded: ["vault"]`,
  `/api/health` `status: degraded`, preflight check `vault: warn`, a `WATCHDOG:` log line — and the
  instance stays in rotation: logins, sessions, SSE and everything database-backed keep working, while
  reading/writing secrets and Vault-stored settings fails cleanly until Vault is back (the watchdog
  probes every 10 s while it is lost and reconnects by itself; a Vault that returns sealed needs
  `LYNDRIX_MASTER_KEY` or the operator). `LYNDRIX_READY_REQUIRES_VAULT=true` restores 503 `vault` — only
  sensible with a highly available Vault. During boot Vault gates readiness with either value. Alert
  on `degraded` / the preflight instead of on readiness.
- **Changing your own password needs `current_password`** (`PATCH /api/users/{u}`, 422 without it).
  lyndrix-ui ≥ 1.8.0 already sends it from the profile (`POST /api/users/{u}/password`); only an admin
  changing their *own* password in the user detail panel (PATCH without `current_password`) gets 422
  — use the profile for that.
- **Environment passwords sign out**: when a seeded account's password from the environment
  (`LYNDRIX_ADMIN_PASSWORD`, `LYNDRIX_BOT_PASSWORD`) differs from the stored one, the boot updates it *and* ends that
  account's sessions. Every instance must carry the same value, or instances booting with different
  values sign the account out in turns.
- **`LYNDRIX_TRUSTED_PROXIES`**: with `*` any client can set `X-Forwarded-For` and pick the address the
  per-address login budgets count against; set the load balancer address/CIDR (k3s: the pod CIDR, e.g.
  `10.42.0.0/16`).
- **Config precedence**: a value in `.env` now wins over a Vault override of the same editable setting
  (documented order OS env > `.env` > Vault > default; before, Vault won over `.env`).
- **`LOG_LEVEL` is honoured from startup**: with the default `INFO` the file log and the in-memory log
  ring no longer collect DEBUG records.
- **Stricter permissions**: `/api/cluster*`, `/docs`, `/redoc`, `/openapi.json` need `admin:read` (docs stay
  open with `ENV_TYPE=dev`); non-admins get a reduced `GET /api/system/config`; `POST /api/system/config`
  accepts only the editable settings (422 otherwise); `admin:write` can no longer manage credentials of
  superadmin accounts or set the system API key.
- **SSE topics are permission-gated**: `plugin:installed`, `plugin:install_failed`,
  `plugin:update_available`, `plugin:operation_changed` need `feature:plugins.manage` or `admin:read`,
  `cluster:*` needs `admin:read`. Non-admins no longer get install toasts.
- **Plugin installs are stricter**: `requirements.txt` may only contain plain requirement specifiers; an
  install/upgrade fails when its dependencies cannot be installed (before: activated without them); repo
  URLs with credentials/query/fragment are rejected. In cluster mode a failure of the network or the
  package index (pip cannot reach PyPI, download 5xx/timeout) is retried after 30, 60, 120 and 300 s —
  the instance stays `plugins:converging` meanwhile, capped by `LYNDRIX_READY_PLUGIN_WAIT_SECONDS` —
  before the long back-off (10 min … 24 h) applies; any other failure backs off at once. Provider tokens
  are never sent over `http://` and
  the GitLab token only to its own host (set `LYNDRIX_PLUGIN_GITLAB_TOKEN_HOST` when several GitLab
  hosts are on `LYNDRIX_PLUGIN_REPO_HOSTS`).
- **Container file modes**: the entrypoint no longer runs `chmod -R 777`; `/data/security` becomes `0700`
  and `vault_keys.enc` `0600` on start.
- **Cluster**: set `stop_grace_period: 30s` (Docker) / check `progressDeadlineSeconds` (k3s); a new pod is
  not ready until its plugins are loaded (`plugins:converging`, capped by
  `LYNDRIX_READY_PLUGIN_WAIT_SECONDS`); `LYNDRIX_INSTANCE_ID` must be unique among running processes;
  rotating `STORAGE_SECRET` needs a restart of all instances. `LYNDRIX_READY_PLUGIN_WAIT_SECONDS` defaults
  to 300 (keep `progressDeadlineSeconds` above it). An instance with role `api` that does not share
  `STORAGE_DIR` (e.g. a Compose core next to the k3s pods) is a permanent preflight `storage: warn` —
  keep it out of the user-facing balancer and narrow `LYNDRIX_STORAGE_SHARED_ROLES`, or share the
  volume. Do not start a rolling restart (`POST /api/system/restart`) or plugin upgrades while 1.11
  and 1.12 instances run side by side. Schema changes are additive and safe next
  to 1.11.x instances during a rolling update (`cluster_leases.token`, `cluster_instances.incarnation`,
  `cluster_instances.stopped_at`, `users.email_trusted`, new table `auth_markers`).
- **Disabled plugins lose their REST routes** (404 until re-enabled); `PUT` plugin settings is a merge.
- Themes uploaded earlier through the NiceGUI page live in the image directory and now count as built-in.

### Security
- Identity linking fails closed: no session is issued for an external login that could not be linked
  (previously a linking error could yield a session for a same-named local account).
- Email auto-linking of LDAP/OIDC identities requires an email set by an admin or a provider; a
  self-service email change loses that trust (`users.email_trusted`).
- Password verification has one choke point for `/api/auth/login`, HTTP Basic, the NiceGUI login and
  LDAP: per-account, per-address and per-pair budgets, bounded Argon2 concurrency
  (`LYNDRIX_LOGIN_VERIFY_CONCURRENCY`), 429/503 with `Retry-After`. HTTP Basic is no longer evaluated on
  `/api/health`, `/api/auth/whoami`, `/api/events` and can be switched off (`LYNDRIX_API_BASIC_AUTH`).
  The cluster preflight warns when `LYNDRIX_TRUSTED_PROXIES` is `*`.
- Scoped API keys are bounded by their scopes even for superadmins and cannot mint keys or change
  credentials. Deleting a user removes their keys, sessions, identities and preferences; keys without
  an owner are rejected; a password change signs out the account's other sessions; SSO-only accounts
  cannot set a local password themselves. Login sessions created from 1.12 on expire after
  `LYNDRIX_SESSION_MAX_AGE_HOURS`; older ones follow `LYNDRIX_SESSION_MAX_AGE_LEGACY`.
- Permission-gated notifications reach only entitled users over SSE; a revoked key or deleted user ends
  its stream within ~20 s; open streams are capped per user (`LYNDRIX_SSE_MAX_CONNECTIONS_PER_USER`).
- OIDC start rejects crafted `redirect_uri`/`next` values and is rate-limited; the auth config API
  accepts only known keys and requires the secret again when the LDAP URL / OIDC issuer changes;
  unseal/init limiter keyed on the real client address plus a global cap; a non-ASCII API key is a 401.
- Vault key material is owner-only on disk; `/api/socket/repair` and the Docker cleanup endpoint validate
  their input (cleanup needs a real prefix and minimum age and never removes the core's container).
- Theme uploads are validated strictly (no markup/CSS injection from a theme; size limits).
- Plugin dependency installs run `pip --isolated` with a minimal environment (no core secrets), new
  `LYNDRIX_PLUGIN_PIP_INDEX_URL`; archive size/file-count limits (`LYNDRIX_PLUGIN_MAX_*`).
- Log masking covers secret-like key names, URL credentials, auth headers, known token prefixes, %-args
  and tracebacks. The cluster bridge keeps events with secret-looking payloads on the emitting instance.
- NiceGUI settings pages no longer send stored secrets to the browser. `requests>=2.32.4`.

### Cluster
- Leases: self-fencing before the TTL can expire for peers, late-returning acquire/renew calls cannot
  resurrect a lost lease, per-acquisition fencing token (`ctx.lease_token(name)`), scoped leases are
  renewed and exclusive within an instance.
- Instance lifecycle: graceful stops leave a tombstone — peers see `draining` / `restarting` instead of
  "no runner instance online" (`LYNDRIX_RESTART_GRACE_SECONDS`); incarnation ids; SIGTERM hands leases
  over at once (`lost` before release) and turns `/api/ready` 503 during the drain; new
  `ctx.instance_state(id)`, `ctx.role_state(role)`, local event `cluster:instance_lost`.
- Readiness: based on live coordinator state (a transient DB error no longer pins 503 for 5 min; a
  starved heartbeat turns the instance unready); preflight runs outside the heartbeat with bounded
  checks; a hung storage mount no longer leaks threads; a `STORAGE_SECRET` mismatch takes only the odd
  instance out; `plugins:converging` until boot plugins are loaded.
- Event bridge: no permanently skipped late-committed ids, ordered publishing with a bounded outbox that
  survives a DB outage, 64 kB payload cap; plugins activated by a peer-triggered reconcile can publish
  cluster events again.
- Rolling restart restarts one instance at a time, leader last — also when it was requested on a
  non-leader (the leader's per-instance commands are no longer dropped as "derived from a relayed
  event"); storage preflight checks marker content among the roles that must share storage
  (`LYNDRIX_STORAGE_SHARED_ROLES`).
- Lease hand-over: a hand-over requested by a peer announces the free lease; a keeper tick or a
  `ctx.lease()` scope during an explicit hand-over (admin release, drain) can no longer take the lease
  back while its release is pending (two holders for up to one heartbeat).
- Preflight: an undecided check gets at most five quick re-runs — heartbeat-triggered ones included
  (before, a permanent `storage: warn` re-ran the preflight on every heartbeat) — then
  `LYNDRIX_PREFLIGHT_INTERVAL_SECONDS` (300); an unchanged warning is logged once.
- Plugin operations: a version that failed verification is backed off (10 min … 24 h) instead of being
  re-installed on every reconcile pass, reverts announce themselves, backed-off instances report at
  once, dead participants are marked `gone`.
- Plugin installs on a fresh instance: transient download/pip failures get four quick retries
  (30/60/120/300 s, own timer) and count as pending for boot convergence; a tracked operation reports
  the instance as failed only when those are used up.
- Readiness no longer depends on Vault after the boot (`LYNDRIX_READY_REQUIRES_VAULT`, default false);
  new preflight check `vault`; `/api/ready` and `/api/health` carry `degraded`.
- A one-shot lease (`ctx.lease()`) granted after its acquire timed out is released at once instead of
  blocking the peers for a TTL; a heartbeat that is still under way can no longer overwrite the stop
  tombstone; the coordinator's worker threads stay below the DB connection pool (12 with the default
  pool of 15, was 16).

### Fixed
- **A database outage no longer signs everybody out.** While the API-key store could not be read, every bearer request
  was answered 401; the React shell drops its token on 401, so each database blip sent all users to the login page.
  The answer is now 503 with `Retry-After: 5`; routes with optional authentication keep serving anonymous callers.
  (Found on the staging cluster, S-3.)
- **No false "dead" verdicts after an outage of our own.** The first instance that reconnected after a cluster-wide
  database outage declared all peers dead (`cluster:instance_lost` for live instances, `instance_state`/`role_state`
  = `dead`). After its own heartbeat failed, an instance now withholds "dead" for one lease TTL plus one beat
  (reported as `unknown`). (Staging, S-2.)
- The preflight reports a coordinator that is still starting as a warning and re-checks, instead of logging
  `PREFLIGHT [coordinator] FAIL … unknown error` on every instance start; a real start error stays a failure.
  (Staging, S-1.)
- Disabling a plugin unmounts its REST routes; a failed import leaves no modules/ORM classes behind;
  async `setup()` runs once; unload/reload awaits `teardown()` (bounded by
  `LYNDRIX_PLUGIN_TEARDOWN_TIMEOUT`) before tasks are cancelled and leases released; several routers
  per plugin; route/package prefix matching with boundaries; locale reload on plugin upgrade.
- Vault read-modify-write never writes after a failed read and uses KV v2 check-and-set
  (`ctx.set_secret`, system config, auth config, plugin settings, custom-repo tokens).
- NotificationRouter sync: coalesced, READ COMMITTED with a short lock wait, writes only changes, never
  deletes rows of installed plugins (the source of the 1205 lock storms); a sync that gave up on a busy
  lock is retried after 60 s (doubling to 10 min) instead of waiting for the next plugin lifecycle event.
- SSE: an overflowing client's stream is closed so it reconnects (no more zombie streams).
- Notifications written during an in-flight flush are persisted; CorrelationStore survives a restart
  (timezone-aware) and its housekeeping loops no longer die on the first error.
- First DB connect retries forever with capped backoff; Vault re-seal after boot is detected
  (`vault:status_changed`, auto-unseal re-requested, hvac timeout 5 s).
- `/api/health`: one shared plugin round, no second `health()` while the previous one still runs.
- Maintenance API: `effective_state` cost for overlapping cron windows (~300 ms → ~3 ms), new
  `MaintenanceState.open_ended` (an open-ended `until` is a lower bound — do not store it).
- Event bus delivers every distinct callback (bound methods of two instances, closures); blocking
  DB/Vault calls moved off the event loop on the login, SSE, plugin lifecycle and webhook paths.
- Preferences lost-update race; bootstrap DDL under the cluster lock; desired-plugin pins match exactly.
- The `users` column migration waits at most 5 s per attempt for the table's metadata lock (6 attempts)
  instead of MariaDB's default of a day, during which every login would queue behind it.
- `requirements.txt`: pip options hidden behind an environment marker are refused.

### Added (plugin API 1.14.0)
- `ctx.aget_secret`, `ctx.aset_secret`, `ctx.aget_setting`, `ctx.aget_all_settings`.
- `ctx.lease_token`, `ctx.instance_state`, `ctx.role_state`, `ctx.incarnation`; `cluster:instance_lost`.
- `module_manager.set_module_active` / `unload_module_async` (await an async teardown).
- Settings: `LYNDRIX_API_BASIC_AUTH`, `LYNDRIX_LOGIN_VERIFY_CONCURRENCY`, `LYNDRIX_SESSION_MAX_AGE_HOURS`,
  `LYNDRIX_SSE_MAX_CONNECTIONS_PER_USER`, `LYNDRIX_PLUGIN_TEARDOWN_TIMEOUT`,
  `LYNDRIX_PLUGIN_GITLAB_TOKEN_HOST`, `LYNDRIX_PLUGIN_PIP_INDEX_URL`, `LYNDRIX_PLUGIN_MAX_ARCHIVE_MB`,
  `LYNDRIX_PLUGIN_MAX_EXTRACTED_MB`, `LYNDRIX_PLUGIN_MAX_ARCHIVE_FILES`, `LYNDRIX_THEME_MAX_ZIP_MB`,
  `LYNDRIX_RESTART_GRACE_SECONDS`, `LYNDRIX_READY_PLUGIN_WAIT_SECONDS`, `LYNDRIX_STORAGE_SHARED_ROLES`,
  `LYNDRIX_RESTART_INSTANCE_TIMEOUT_SECONDS`, `LYNDRIX_PREFLIGHT_INTERVAL_SECONDS`,
  `LYNDRIX_SESSION_MAX_AGE_LEGACY` (`exempt` | `from_enforcement` | `strict`),
  `LYNDRIX_READY_REQUIRES_VAULT` (default `false`) — see `docs/ha/configuration.md`,
  `docs/security.md` and `docs/deployment.md`.
- `GET /api/users/{u}/api-keys`: `is_session`, `expires_at`, `expiry` per key (additive);
  `GET /api/ready` and `GET /api/health`: `degraded` (additive).

### Known limits
- MariaDB/Galera-, NFS- and Vault-specific paths (READ COMMITTED sessions, `GET_LOCK` skip,
  `FOR UPDATE`, KV v2 CAS, `ALTER TABLE` upgrade, lease `UPDATE` order) are covered by fakes and SQLite
  only — run the HA dev bench (`docs/ha/operations/test-plan.md`) before releasing.

## [1.11.1] - 2026-11-13
### Fixed
- Database connections against a silently dead peer (DB VIP failover, half-open TCP) no longer block
  for the kernel retransmit timeout (~16 min with `tcp_retries2=15`): the engine sets pymysql
  `read_timeout`/`write_timeout` on every pooled connection (new `DB_SOCKET_TIMEOUT_SECONDS`,
  `0` = auto = `DB_STATEMENT_TIMEOUT_SECONDS` + 30 s, at least 90 s; a value not above the statement
  cap is raised so the server-side cap always fires first). The failing query surfaces as error 2013,
  SQLAlchemy discards the connection and the next call uses a fresh one; `pool_pre_ping` is bounded
  by the same timeout.
- Cluster heartbeat: one hung beat no longer parks the heartbeat task until the socket times out.
  Each beat is abandoned after the lease TTL (`heartbeat_timeout_s`), the next one runs on a fresh
  connection, and the failure is logged with its duration, the time since the last successful beat
  and the TTL after which peers report the instance dead (every one of the first three failures,
  then about once a minute), followed by a recovery line. A successful beat that took longer than
  the interval logs a latency warning. `GET /api/cluster` → `coordinator.heartbeat` carries
  `last_ok_at`, `failures`, `last_duration_s`, `last_error`.
- Lease renewals (`LeaseKeeper.tick`) are bounded by the lease TTL as well, so fencing ("could not be
  renewed for N s — treating it as lost") happens on time instead of after the TCP timeout.

### Operations
- Recommended alongside: `net.ipv4.tcp_retries2=8` on the core/runner containers or pods (namespaced
  `net.*` sysctl, no extra capability) — see `docs/ha/building-blocks/database.md`.
