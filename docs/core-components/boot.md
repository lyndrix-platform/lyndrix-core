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
