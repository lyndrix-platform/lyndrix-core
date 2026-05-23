# Vault Component

## Purpose

The Vault component is the secret-management bootstrap layer of Lyndrix Core. It decides whether Vault needs initialization, unseal, token restoration, or mount preparation before the rest of the platform can continue.

## Main locations

- `app/core/components/vault/logic/vault_service.py`
- `app/core/components/vault/logic/auto_unseal.py`
- `app/core/components/vault/logic/vault_init.py`
- `app/core/components/vault/logic/crypto.py`
- `app/core/components/vault/ui/routes.py`

## Responsibilities

- react to `system:started`
- determine whether Vault is initialized and unsealed
- handle init and unseal requests
- restore the stored root token when possible
- ensure the `lyndrix` KV v2 mount exists
- emit readiness signals for downstream components
- provide setup and unseal UI flows

## Events

### Subscribes

- `system:started`
- `vault:init_requested`
- `vault:unseal_requested`
- `vault:needs_init`
- `vault:needs_unseal`

### Emits

- `vault:needs_init`
- `vault:needs_unseal`
- `vault:auth_failed`
- `vault:opened`
- `vault:ready_for_data`
- `vault:init_requested`
- `vault:unseal_requested`

## Runtime behavior

At startup, the component:

1. checks whether Vault is initialized
2. checks whether Vault is sealed
3. restores the root token from encrypted key material when possible
4. ensures the `lyndrix` mount exists as a KV v2 secret engine
5. emits `vault:opened`
6. emits `vault:ready_for_data`

If access to the mount cannot be prepared, the component emits `vault:auth_failed` and blocks downstream startup.

## UI endpoints

- `/setup`
- `/unseal`

## Operational notes

- `vault_keys.enc` must be protected and backed up
- `LYNDRIX_MASTER_KEY` controls whether encrypted bootstrap material can be reused
- the database and plugin secret workflows depend on successful Vault preparation
