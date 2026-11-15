# Security and Vault

This page describes the security model used by Lyndrix Core, with a focus on Vault, bootstrap secrets, and operational hardening.

## Security model overview

Lyndrix Core follows a **Vault-centered secret model**.

Core principles:

- secrets should live in HashiCorp Vault, not in source-controlled files
- Vault readiness controls when downstream services are allowed to initialize
- plugins access secrets through isolated namespaces
- bootstrap data on disk is encrypted and requires a master key for recovery

## Vault as the source of truth

Lyndrix stores secrets in the `lyndrix/` mount using KV v2.

Typical secret consumers include:

- core authentication settings
- plugin-specific secret values
- optional runtime tokens such as GitHub credentials

The Vault service ensures that the `lyndrix` mount exists before the platform continues booting.

## Encrypted `vault_keys.enc`

Vault bootstrap material is persisted in `vault_keys.enc` and is encrypted before being written to disk.

Implementation details:

- key derivation: **Argon2id**
- encryption: **AES-GCM**
- blob layout: `salt[16] + nonce[16] + tag[16] + ciphertext[n]`

Related configuration in `app/config.py`:

- `LYNDRIX_ARGON_TIME`
- `LYNDRIX_ARGON_MEM`
- `LYNDRIX_ARGON_PARALLEL`
- `LYNDRIX_MASTER_KEY`

Without the correct master key, the encrypted Vault bootstrap material cannot be decrypted.

## Vault lifecycle

At startup, Vault participates in the boot sequence through a fixed flow.

Typical lifecycle:

1. `system:started` is emitted by the application
2. the Vault service checks whether Vault is initialized
3. if uninitialized, Lyndrix emits `vault:needs_init`
4. if sealed, Lyndrix emits `vault:needs_unseal`
5. once available, Lyndrix restores the token if possible, ensures the secret mount, and emits `vault:opened`
6. after the mount is ready for data access, Lyndrix emits `vault:ready_for_data`

If `LYNDRIX_MASTER_KEY` is configured, Lyndrix can participate in auto-init or auto-unseal flows.

## Plugin secret isolation

Plugins do not read or write directly against arbitrary Vault locations through the public API.

Instead, they use:

- `ctx.get_secret(key)`
- `ctx.set_secret(key, value)`

The core maps these calls to per-module paths:

- `core/<manifest.id>` for core modules
- `plugins/<manifest.id>` for plugins

This keeps module data separated by design and reduces accidental secret overlap.

## Database and runtime security behavior

The database service also contains security-relevant runtime behavior:

- it uses `pool_pre_ping=True` for connection validation
- it distinguishes between transient connectivity issues and permanent configuration failures
- it redacts connection-style credentials from logged DB errors
- it emits `system:maintenance_mode` when the platform should not continue normally

## Authentication-related considerations

Authentication bootstraps from environment-backed defaults and upgrades into the configured provider chain.

Important points:

- default admin and bot passwords are convenient for development only
- LDAP and OIDC provider secrets can be loaded from Vault
- provider activation is driven by `LYNDRIX_AUTH_PROVIDERS`
- plugins can register providers dynamically, but the provider chain still controls activation order

## Access control (1.0)

Authorization is **group-based RBAC**, resolved server-side for every request
(see [Auth](core-components/auth.md)):

- Effective permissions come from **group membership** (`INT_ADMIN` / `INT_USER` / `INT_VIEWER` are seeded) plus per-user direct grants — never from raw role strings. The React UI mirrors this via `GET /api/me/access` but enforcement is always server-side.
- **`superadmin` is a break-glass bypass, disabled by default.** Do not rely on it for day-to-day admin; grant `INT_ADMIN` instead. Enable it only deliberately via `LYNDRIX_ADMIN_FORCE_SUPERADMIN=true`.
- **Per-plugin API scoping**: prefer `plugin:<id>:api:read` / `:api:write` (and plugin-declared custom permissions) over the broad global `api:read` / `api:write`, so a token/group can be limited to individual plugins.
- **Notification visibility** can be gated per endpoint by permission id, so sensitive events (e.g. plugin lifecycle) are only visible to admins.

### Password logins

Every password check goes through one choke point (`login_guard`): the login
form (`POST /api/auth/login`, NiceGUI), HTTP Basic on API routes and "confirm
your current password". Only **verified wrong passwords** are counted; an
attempt that was turned away never extends a block, so every block ends by
itself. The response is `429` + `Retry-After` (never `401`) while blocked.

| Budget | Limit | Purpose |
|---|---|---|
| account + client address | 5 failures / minute, 10 / 10 minutes | plain guessing from one address |
| account, addresses that already failed on it | 20 failures / 10 minutes | guessing spread over a few addresses |
| account, addresses without a failure of their own | 10 more / 10 minutes | room for the real user while the account is under attack |
| client address, any account | 30 failures / minute | one password sprayed over many accounts; load on the directory |

Rules that keep legitimate clients working:

- An account + address that produced the **correct** password within the last 30 days is only checked against its own budget (first row) — other people's failures do not block it.
- The **same wrong password** repeated (a script with an outdated password) counts once per 10 minutes and costs one Argon2 run, so it does not block the account for correctly configured clients.
- A confirmed password is remembered while it keeps being used (5 minutes idle, bound to the stored hash), so HTTP Basic costs one Argon2 run, not one per request.
- An unreachable directory is not a wrong password: such attempts use the address budget only, so an LDAP outage does not leave its users blocked afterwards. A local account's wrong password counts regardless.
- At most `LYNDRIX_LOGIN_VERIFY_CONCURRENCY` (4) Argon2 verifications run at once; the rest wait up to 5 s and then get `503` + `Retry-After`.

What an attacker can still do — the price of a hard ceiling on guesses per account:

- **Many addresses** (botnet, or a forgeable `X-Forwarded-For`, see below): about 30 guesses per account, 10 minutes and instance. While that budget is full, the account's password login is refused from addresses that never logged in successfully — for as long as the attack lasts plus at most 10 minutes. Known-good addresses, existing sessions and API keys keep working.
- **One shared address** (clients behind a load balancer the core does not trust, NAT): they share the account + address budget, so anybody there can keep an account's password login blocked *from that address* while they keep failing. Configure `LYNDRIX_TRUSTED_PROXIES` so the core sees real client addresses.
- The counters are **per instance** (only the login form's 5-per-minute counter is shared through the cluster coordinator): with N instances behind the balancer the budgets are up to N times larger.

`LYNDRIX_TRUSTED_PROXIES=*` makes the core believe `X-Forwarded-For` from every
sender. That is fine while only the load balancer can open a connection to the
core; if the core port is reachable in any other way (other pods, a published
port), a client chooses the address the limiters see, and only the per-account
ceiling above holds. Prefer the balancer's address or CIDR; the preflight
reports `*` as a warning.

### External identity linking

Each login resolves to one local profile via a stable identity link
(`UserIdentity`). Auto-matching an external account to an existing user by email
only happens when the provider **vouches** for it — AD/LDAP directory mail, or an
OIDC `email_verified: true` claim. An unverified or ambiguous email never merges
accounts; a new profile is created instead (spoofing protection). Auto-matching
additionally needs a local email that an administrator or a provider put on the
profile — an address a user typed in themselves is never a link target.

The OIDC redirect flow uses **PKCE between the core and the IdP** and a
single-use, server-side state. The SPA start endpoint only accepts a plain
callback URL on an allowed origin (no credentials, query or fragment), an
in-app `next` path, and is rate-limited. Not yet bound to the browser that
started the flow (login CSRF): PKCE between SPA and core and a nonce cookie for
the NiceGUI flow are open.

### Session tokens

Web logins mint a `UserApiKey` labelled `web-session · <browser> on <os> · <time>`.
Users can review and revoke their **active sessions** individually (or "sign out
all other sessions") from Settings → Profile, separate from long-lived API keys.

- A login session expires `LYNDRIX_SESSION_MAX_AGE_HOURS` after it was created
  (default 720 = 30 days, `0` = never): the token then answers `401` and the
  shell returns to the login page. Named API keys do not expire. The label
  prefix `web-session` is reserved for sessions.
- **Sessions from before the rule are grandfathered.** The first boot of a core
  that knows the max-age (≥ 1.12) records that instant once for the whole
  deployment (table `auth_markers`, row `session_max_age_enforced_from`; written
  under the bootstrap lock, never changed afterwards, the same for every
  instance). A session created before it is a *legacy* session and
  `LYNDRIX_SESSION_MAX_AGE_LEGACY` decides what the max-age means for it:

  | Value | Legacy sessions |
  |---|---|
  | `exempt` (default) | never expire by age — an upgrade signs nobody out and a session token used by a script keeps working |
  | `from_enforcement` | age counts from the recorded instant: they all end one max-age after the upgrade |
  | `strict` | like any other session: age counts from creation, old ones end with their next request |

  The exemption covers the age only — revoking the session, a password change
  and deleting the account end a legacy session like any other. While the
  instant cannot be read (bootstrap not finished, table missing) no session is
  expired, except with `strict`, which does not need it. `GET
  /api/users/{username}/api-keys` shows it per row: `is_session`, `expires_at`
  (null = does not expire by age) and `expiry` (`max_age`, `legacy_exempt`,
  `legacy_from_enforcement`, `disabled`, `unknown`). To end the exemption later,
  set `from_enforcement` (announced end) or `strict` (immediately) on every
  instance; long-lived automation should use a named API key either way.
- A password change (own, admin reset, environment rotation) ends the account's
  other login sessions; API keys are kept.
- Deleting an account removes its API keys, sessions, linked identities and
  preferences in the same transaction; a key whose owner is gone is rejected and
  is never inherited by a later account of the same name.
- An account without a local password (SSO/LDAP-only) cannot set one itself —
  an administrator can.
- **Scoped API keys** are bounded by their scopes whoever owns them (also for a
  superadmin) and cannot create keys, change a password or an email address, or
  revoke anything but themselves.

## Hardening recommendations

For any non-local environment, apply these minimum controls:

- replace all default secrets and bootstrap passwords
- manage `LYNDRIX_MASTER_KEY` through a secure secret manager or HSM-backed process
- expose Vault UI and Lyndrix only behind TLS and trusted network boundaries
- back up MariaDB, Vault storage, and `vault_keys.enc`
- test restore procedures regularly
- review application logs to ensure secrets are never logged
- protect access to the host paths that store Vault and database data

## Security-relevant events

Important topics to watch during operations:

- `vault:init_requested`
- `vault:unseal_requested`
- `vault:auth_failed`
- `vault:opened`
- `vault:ready_for_data`
- `system:maintenance_mode`
- `db:connected`

These events are useful when building monitoring, dashboards, or operational playbooks.

## Operational guidance

### Before upgrades

Always back up:

- database data
- Vault storage
- `vault_keys.enc`

### After restarts

Verify:

- Vault is unsealed and reachable
- the `lyndrix` mount is available
- the database connects successfully
- login works for the intended provider chain
- plugins that depend on Vault secrets become active

### If Vault authentication fails

Check:

- whether the stored token can still access the `lyndrix` mount
- whether the master key used for decryption is correct
- whether Vault policies allow mount inspection and KV operations
