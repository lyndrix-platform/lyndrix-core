# System Monitoring Component

## Purpose

The System Monitoring component continuously collects host metrics and emits them on the bus.

## Main location

- `app/core/components/system/logic/monitor_service.py`

## Responsibilities

- Start metrics loop at system startup
- Collect CPU, RAM, and disk metrics (via `psutil`)
- Emit metrics updates every ~2 seconds
- Handle transient/fatal collection failures safely

## Events

### Subscribes

- `system:started`

### Emits

- `system:metrics_update`
