# System Monitoring Component

## Purpose

The System Monitoring component continuously samples host metrics and publishes them on the event bus.

## Main location

- `app/core/components/system/logic/monitor_service.py`

## Responsibilities

- start metric collection on `system:started`
- collect CPU, RAM, and disk usage through `psutil`
- emit updates roughly every two seconds
- tolerate transient collection failures without crashing the app
- stop after repeated fatal collection errors

## Events

### Subscribes

- `system:started`

### Emits

- `system:metrics_update`

## Runtime behavior

The monitoring loop keeps an internal failure counter. Temporary `psutil` or OS errors are logged and retried, while repeated failures eventually stop the loop cleanly.

If `psutil` is not installed, the component logs a warning and skips metric collection instead of crashing startup.
