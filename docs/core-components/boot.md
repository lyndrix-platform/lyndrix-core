# Boot Component

## Purpose

The Boot component is the startup orchestrator that turns core readiness into module loading and finally into a usable system state.

## Main location

- `app/core/components/boot/logic/boot_service.py`

## Responsibilities

- wait for `iam:ready`
- maintain the boot phase state machine
- trigger module discovery and loading through `ModuleManager`
- publish boot progress to the UI and other listeners
- activate maintenance mode on fatal startup failure

## Boot phases

The component publishes these phases:

- `waiting_core`
- `loading_modules`
- `ready`
- `failed`

The emitted `system:boot_phase` payload includes:

- `phase`
- `error`
- `is_booting`

## Events

### Subscribes

- `iam:ready`
- `iam:failed` — marks the IAM dependency as failed on the boot screen

### Emits

- `system:boot_phase`
- `system:boot_complete`
- `system:maintenance_mode`

## Runtime behavior

When `iam:ready` is received, the component:

1. switches to `loading_modules`
2. imports the module manager lazily to avoid circular imports
3. runs `module_manager.load_all()`
4. waits briefly for async setup follow-up work
5. switches to `ready`
6. emits `system:boot_complete`

If module loading fails, the component records the error, emits the `failed` phase, and activates maintenance mode.

## Boot status endpoint and boot screens

`GET /api/boot` is public and reachable before Vault, the database and IAM are up (the boot
interceptor lets every `/api` path through). It returns `BootService.snapshot()`: the boot phase,
`is_booting` / `ready` / `failed`, the deployment `mode` (`single` or `cluster`,
from `LYNDRIX_CLUSTER_MODE`), `vault_ui_state`, the elapsed time and one row per dependency the
core needs:

| id | What it reports |
|---|---|
| `vault` | `pending` (connecting), `action` (`needs_init` / `sealed`), `online`, `failed` |
| `database` | `waiting` for Vault, `pending` with retry progress (`db_retry` attempt/max, `reconnecting`), `online`, `failed` (`db_config_error`, `db_gave_up`) |
| `iam` | `waiting` for the database, `pending` (`iam_bootstrapping`), `online`, `failed` |
| `modules` | `waiting` for IAM, `pending` while `load_all()` runs, `online` with active/failed/total counts, `failed` |
| `coordinator` | cluster mode only: `waiting` for the database, `pending` while starting, `online` (`coordinator_connected` with the driver), `failed` |
| `leader` | cluster mode only: `waiting` for the coordinator, `pending` (`leader_electing`), `online` (`leader_self` / `leader_other` with the instance id) |
| `peers` | cluster mode only: `online` with the number of other live instances |
| `storage` | cluster mode only: `unavailable` — no shared storage driver in this core version |

Each row carries `status` (`online | pending | waiting | action | failed | unavailable`), a
`detail` key with `detail_params`, and `since` (last status change). The payload is built from
in-memory flags only — no Vault or DB round-trip, no error text — so every open browser tab can
poll it once a second during boot.

Consumers:

- the React shell (`lyndrix-ui`, `BootGate` / `BootScreen`) replaces the app with the boot screen
  while the core boots, has failed, or is unreachable (restart), polling every 1.5 s and falling
  back to a 20 s heartbeat once ready so a restart brings the screen back by itself; labels are
  translated from the `boot.*` keys of the `ui` namespace;
- the NiceGUI entry page (`/`) renders the same list with Material icons, refreshed once a second,
  until the core releases the UI.

