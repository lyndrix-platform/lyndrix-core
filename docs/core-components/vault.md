# Vault Component

## Purpose

The Vault component is the secret-management bootstrap layer of Lyndrix Core.

## Main locations

- `app/core/components/vault/logic/vault_service.py`
- `app/core/components/vault/logic/auto_unseal.py`
- `app/core/components/vault/ui/routes.py`

## Responsibilities

- Check Vault initialization/seal state
- Handle init/unseal requests
- Restore token and ensure `lyndrix` KV v2 mount
- Emit readiness events used by downstream components
- Provide setup and unseal UI flows

## Events

### Subscribes

- `system:started`
- `vault:init_requested`
- `vault:unseal_requested`
- `vault:needs_init` (auto-unseal manager)
- `vault:needs_unseal` (auto-unseal manager)

### Emits

- `vault:needs_init`
- `vault:needs_unseal`
- `vault:auth_failed`
- `vault:opened`
- `vault:ready_for_data`
- `vault:init_requested` (auto-unseal manager)
- `vault:unseal_requested` (auto-unseal manager)

## UI endpoints

- `/setup`
- `/unseal`
