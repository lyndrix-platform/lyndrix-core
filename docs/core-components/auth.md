# Auth Component

## Purpose

The Auth component initializes identity and access management (IAM), seeds core users, and manages the runtime auth provider chain.

## Main locations

- `app/core/components/auth/logic/auth_service.py`
- `app/core/components/auth/logic/providers/*`
- `app/core/components/auth/ui/routes.py`

## Responsibilities

- Wait for DB readiness (`db:connected`)
- Create/migrate IAM schema
- Seed admin + bot users
- Initialize provider chain (`local`, `ldap`, `oidc`, plugin providers)
- Emit IAM readiness (`iam:ready`)

## Events

### Subscribes

- `db:connected`
- `auth:register_provider` (provider registry)

### Emits

- `iam:ready`

## UI/API endpoints

- `/login`
- `/auth/callback/oidc`
- `/auth/complete`
