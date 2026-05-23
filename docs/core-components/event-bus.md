# Event Bus

The Lyndrix Event Bus (`app/core/bus.py`) is the global async event backbone of the platform.

## What it is

- A topic-based publish/subscribe bus (`topic: payload`)
- Shared by core components and plugins
- Supports sync and async handlers
- Tracks async tasks and logs handler failures centrally

## Core API

- `bus.subscribe(topic)` → decorator to register a handler
- `bus.emit(topic, payload)` → publish an event
- `bus.create_tracked_task(coro, name=...)` → start observable background task

## Plugin integration

Plugins should integrate through `ModuleContext` (`ctx`) instead of directly using the global bus:

- `ctx.subscribe(topic)`
- `ctx.emit(topic, payload)`
- `ctx.create_task(coro, name=...)`

This enforces each plugin manifest permission model:

- `permissions.subscribe`
- `permissions.emit`

If a plugin tries to emit/subscribe to a non-permitted topic, Lyndrix blocks the action and logs a warning.

## Recommended plugin flow

1. Declare event permissions in the plugin manifest
2. Register subscriptions in `setup(ctx)`
3. Emit events only through `ctx.emit(...)`
4. Use `ctx.create_task(...)` for long-running async work
5. Keep payloads minimal, stable, and JSON-serializable

## Event naming convention

Use namespaced topics (`domain:event_name`) such as:

- `vault:ready_for_data`
- `plugin:installed`
- `git:status_update`

For all currently used topics, see [Event Catalog](events.md).
