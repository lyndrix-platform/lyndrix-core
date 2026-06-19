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
- seed admin and bot users from environment-backed values
- initialize configured auth providers such as `local`, `ldap`, and `oidc`
- allow plugins to register additional providers at runtime
- emit `iam:ready` when authentication is usable

## Configuration touchpoints

Important settings live in `app/config.py`, including:

- `LYNDRIX_ADMIN_USER`, `LYNDRIX_ADMIN_PASSWORD`
- `LYNDRIX_BOT_USER`, `LYNDRIX_BOT_PASSWORD`
- `LYNDRIX_AUTH_PROVIDERS`
- LDAP settings such as `LYNDRIX_LDAP_URL`
- OIDC settings such as `LYNDRIX_OIDC_ISSUER`

## Events

### Subscribes

- `db:connected`
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

- `/login`
- `/auth/callback/oidc`
- `/auth/complete`

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

`ApiIdentity.allows(permission)` evaluates authorization: the system key and the `superadmin` role bypass checks; otherwise the owner's effective permissions are resolved through the group/permission system (Permissions tab), and scoped per-user keys additionally require the permission to be within their `key_scopes`.

### Swagger / OpenAPI

The interactive API docs include an **Authorize** button so you can paste a system or per-user API key (or Bearer token) and exercise protected endpoints directly.

### Configuration touchpoint

- `LYNDRIX_SYSTEM_API_KEY` — master machine-to-machine key (see [Installation](../deployment.md)).

## Operational notes

- default bootstrap passwords are convenient for development but unsafe for shared environments
- LDAP and OIDC credentials can be pulled from Vault-backed settings
- provider reinitialization is supported at runtime after settings changes
- per-user API keys are generated from the Settings UI; treat the system key as a privileged master credential and store it in Vault or an env secret
