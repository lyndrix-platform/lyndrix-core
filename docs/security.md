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

### External identity linking

Each login resolves to one local profile via a stable identity link
(`UserIdentity`). Auto-matching an external account to an existing user by email
only happens when the provider **vouches** for it — AD/LDAP directory mail, or an
OIDC `email_verified: true` claim. An unverified or ambiguous email never merges
accounts; a new profile is created instead (spoofing protection). The OIDC
redirect flow uses **PKCE + a session-bound state**.

### Session tokens

Web logins mint a `UserApiKey` labelled `web-session · <browser> on <os> · <time>`.
Users can review and revoke their **active sessions** individually (or "sign out
all other sessions") from Settings → Profile, separate from long-lived API keys.

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
