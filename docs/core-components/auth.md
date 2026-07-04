# Auth Component

## Purpose

The Auth component initializes identity and access management (IAM), creates required schema objects, seeds bootstrap users, and prepares the runtime authentication provider chain.

## Main locations

- `app/core/components/auth/logic/auth_service.py`
- `app/core/components/auth/logic/providers/*`
- `app/core/components/auth/ui/routes.py`
- `app/core/components/auth/ui/login_ui.py`

## Responsibilities

- wait for `db:connected`
- create or validate IAM tables through SQLAlchemy metadata
- apply lightweight idempotent schema migrations for selected columns
- seed the built-in groups (`INT_ADMIN` / `INT_USER` / `INT_VIEWER`) and the admin/bot users
- initialize configured auth providers such as `local`, `ldap`, and `oidc`
- link every login (local or SSO) to a stable local profile via the identity service
- apply manifest-declared plugin roles + baseline per-plugin grants after boot
- allow plugins to register additional providers at runtime
- emit `iam:ready` when authentication is usable

## Identity & Permissions 2.0 (1.0)

The identity model was reworked for the 1.0 release.

### Users & identities

- **`User.id` is a stable UUID string** — the global identifier that survives username or email changes. `hashed_password` is nullable (SSO-only users have none).
- **`User.roles` carries system flags only** (`superadmin`, `bot`); it is no longer matched against group names.
- **`User.groups`** is the sole membership field. Effective permissions =
  `union(Group.permissions for the user's groups) ∪ User.extra_permissions`,
  resolved centrally by `access_service` (short-TTL cached).
- **`UserIdentity`** links a local user to one external account
  `(provider, provider_user_id)` — AD `objectSid` (preferred) / DN for LDAP,
  `sub` for OIDC, username for local. One person logging in through several
  providers resolves to **one** local profile with N linked identities.

### Default groups (seeded, create-if-missing)

| Group | Grants |
|---|---|
| `INT_VIEWER` | `feature:dashboard.view`, `api:read` |
| `INT_USER` | + `api:write`, `feature:dashboard.admin_sections` |
| `INT_ADMIN` | all `feature:*` + `api:read`/`api:write` |

The admin account is an ordinary `INT_ADMIN` member. `superadmin` is a
break-glass bypass, **off by default** — enable it explicitly with
`LYNDRIX_ADMIN_FORCE_SUPERADMIN=true`. Existing groups are never overwritten on
restart, so operator edits survive.

### Permission taxonomy

| id | meaning |
|---|---|
| `feature:<key>` | core UI feature gate |
| `api:read` / `api:write` | global read/write (permanent compatibility fallback) |
| `route:<ui_route>` | a NiceGUI page |
| `plugin:<id>` | the plugin is visible/usable |
| `plugin:<id>:route:<path>` | one React route of a plugin |
| `plugin:<id>:api:read` / `:api:write` | per-plugin API granularity (auto-registered) |
| `plugin:<id>:<custom>` | a plugin-declared fine-grained permission |

`access_service.has_permission()` implements the fallback rule: a caller holding
the global `api:read`/`api:write` satisfies any `plugin:<id>:api:read`/`:write`,
so grants made before plugins adopted namespaced guards keep working.

### Roles (manifest-declared)

Roles are **not** database entities. A plugin declares roles in its manifest
(`ManifestRole`: a named permission bundle plus optional `auto_map_groups`); the
`role_registry` applies each role's permissions to its target groups **exactly
once**, tracked in `role_grant_ledger` so a later admin revocation is never
re-applied. Baseline per-plugin visibility (`plugin:<id>` + `:api:read` to all
default groups, `:api:write` to `INT_USER`/`INT_ADMIN`) is granted the same way
on `system:boot_complete` and `plugin:state_changed`.

## Configuration touchpoints

Important settings live in `app/config.py`, including:

- `LYNDRIX_ADMIN_USER`, `LYNDRIX_ADMIN_PASSWORD`
- `LYNDRIX_BOT_USER`, `LYNDRIX_BOT_PASSWORD`
- `LYNDRIX_ADMIN_FORCE_SUPERADMIN` — opt in to the break-glass `superadmin` flag (default off)
- `LYNDRIX_SSO_DEFAULT_GROUPS` — groups assigned to a newly auto-created SSO user (default `INT_VIEWER`)
- `LYNDRIX_AUTH_PROVIDERS` — ordered provider chain (`local`, `ldap`, `oidc`)
- LDAP settings such as `LYNDRIX_LDAP_URL` (AD `objectSid` is read for stable linking)
- OIDC settings such as `LYNDRIX_OIDC_ISSUER` (the redirect flow uses PKCE + a session-bound state; trusted-email linking honours the `email_verified` claim)

## Events

### Subscribes

- `db:connected`
- `system:boot_complete`, `plugin:state_changed` (apply role/baseline grants once manifests are loaded)
- `auth:register_provider`

### Emits

- `iam:ready`

## Runtime behavior

When the database becomes ready, the component:

1. creates missing IAM tables
2. applies selected additive schema migrations
3. seeds or updates bootstrap users
4. constructs the active provider chain
5. emits `iam:ready`

This event is the release signal used by the Boot component.

## UI and auth endpoints

- `/login` (NiceGUI) · `/auth/callback/oidc` · `/auth/complete` (NiceGUI SSO handoff)

### React SPA auth (1.0)

The React frontend authenticates through the same provider chain:

- `POST /api/auth/login` runs the **full provider chain** (local + LDAP share the credentials form) and returns a `web-session` token.
- `GET /api/auth/providers` — unauthenticated discovery the login page reads *before* login; each `redirect`-kind provider renders an SSO button.
- `GET /api/auth/oidc/start` → IdP → callback mints the session token server-side and hands the SPA a **one-time exchange code** (`POST /api/auth/token-exchange`) — the token is never placed in a redirect URL.
- `GET /api/me/access` — the server-derived effective-access view (`permissions[]` + per-plugin `visible`/`can_read`/`can_write`/`routes`) the React shell uses to gate the sidebar, routes and dashboard sections. All enforcement stays server-side; the UI only decides what to render.

## API authentication and authorization

Beyond the interactive login flow, Lyndrix Core ships a small, composable auth layer for HTTP/API endpoints (`app/core/api/security.py`), exported from `core.api`. It accepts several credential mechanisms and resolves them to a single `ApiIdentity`.

### Credential methods

`authenticate_request()` tries each method in order and returns the first identity found:

1. **System API key** — header `X-API-Key: <key>` or `Authorization: Bearer <key>`. Resolved from `LYNDRIX_SYSTEM_API_KEY` first, then Vault (`lyndrix/core/settings → system_api_key`). The master key bypasses all permission checks (`is_system=True`).
2. **Per-user API key** — same headers, validated against the per-user key store; carries the owner's roles/permissions and may be limited to an explicit set of `key_scopes`.
3. **HTTP Basic** — `Authorization: Basic <base64(user:pass)>`, validated against the local IAM.
4. **Session cookie** — an already-authenticated NiceGUI dashboard session, when the request runs inside NiceGUI's storage context.

> If neither `LYNDRIX_SYSTEM_API_KEY` nor a Vault-stored `system_api_key` is configured, the **system-key method is disabled entirely** — no empty or implicit key is ever accepted. A fresh install with the key unset is simply not reachable via the system API key.

### Resolution order for the system key

`LYNDRIX_SYSTEM_API_KEY` (env) → Vault `lyndrix/core/settings → system_api_key` → disabled. A configured-but-empty value is treated as *not configured*. The key can also be set from the Settings UI, which persists it to Vault.

### Protecting endpoints

Use the FastAPI dependencies exported from `core.api`:

```python
from core.api import require_api_auth, require_permission, optional_api_auth, ApiIdentity

# Require any valid identity (401 if missing)
@router.get("/secure")
async def secure(identity: ApiIdentity = Depends(require_api_auth)):
    return {"who": identity.username, "via": identity.method}

# Require a specific permission (401 if unauthenticated, 403 if not allowed)
@router.post("/config")
async def update(identity: ApiIdentity = Depends(require_permission("api:write"))):
    ...

# Public, but surface the caller when present
@router.get("/public")
async def public(identity = Depends(optional_api_auth)):
    ...
```

`ApiIdentity.allows(permission)` evaluates authorization: the system key and the `superadmin` system flag bypass checks; otherwise the caller's **precomputed effective permission set** (groups-union ∪ `extra_permissions`, resolved by `access_service` in every `_try_*` credential builder) decides — including the per-plugin fallback. Scoped per-user keys additionally require the permission to be within their `key_scopes`.

> **1.0 fix:** earlier versions only read `User.roles` on the API path, so assigning a user to a group did nothing over HTTP. Since 1.0 the effective set is resolved from group membership for *every* credential method.

### Swagger / OpenAPI

The interactive API docs include an **Authorize** button so you can paste a system or per-user API key (or Bearer token) and exercise protected endpoints directly.

### Configuration touchpoint

- `LYNDRIX_SYSTEM_API_KEY` — master machine-to-machine key (see [Installation](../deployment.md)).

## Operational notes

- default bootstrap passwords are convenient for development but unsafe for shared environments
- LDAP and OIDC credentials can be pulled from Vault-backed settings
- provider reinitialization is supported at runtime after settings changes
- per-user API keys are generated from the Settings UI; treat the system key as a privileged master credential and store it in Vault or an env secret
