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

## Operational notes

- default bootstrap passwords are convenient for development but unsafe for shared environments
- LDAP and OIDC credentials can be pulled from Vault-backed settings
- provider reinitialization is supported at runtime after settings changes
