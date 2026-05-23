# Boot Component

## Purpose

The Boot component is the central startup orchestrator that transitions Lyndrix through boot phases until the system is ready.

## Main location

- `app/core/components/boot/logic/boot_service.py`

## Responsibilities

- Wait for `iam:ready`
- Transition boot phases (`waiting_core` → `loading_modules` → `ready` / `failed`)
- Trigger module loading through `ModuleManager`
- Emit global boot lifecycle events

## Events

### Subscribes

- `iam:ready`

### Emits

- `system:boot_phase`
- `system:boot_complete`
- `system:maintenance_mode` (on boot failure)
