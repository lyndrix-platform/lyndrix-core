# Event Bus

The Lyndrix Event Bus in `app/core/bus.py` is the async backbone shared by core components and plugins.

## Purpose

The bus provides a lightweight publish/subscribe model that lets components communicate without hard-coding direct dependencies.

It is used for:

- startup sequencing
- readiness signaling
- plugin lifecycle reactions
- operational notifications
- background task tracking

## Core API

Main primitives:

- `bus.subscribe(topic)` — decorator that registers a handler
- `bus.emit(topic, payload)` — publish an event with an optional payload
- `bus.create_tracked_task(coro, name=...)` — create an observable background task

## Handler model

The bus supports both:

- synchronous handlers
- asynchronous handlers

Async handlers are wrapped in tracked tasks so that failures are logged centrally rather than disappearing silently.

## Logging behavior

The bus also acts as a logging choke point for runtime events.

Important built-in behaviors:

- very noisy topics such as `system:metrics_update` are logged at lower verbosity
- sensitive topics such as Vault init and unseal requests are redacted
- selected large payloads are summarized instead of logged verbatim

That keeps logs useful while reducing accidental data exposure.

## Duplicate subscription protection

The bus tracks subscriber keys per topic and ignores duplicate registrations of the same callback. This helps during reload-heavy development flows and prevents repeated event handling.

## Plugin integration

Plugins should not normally import the global bus directly.

Instead, they should use the `ModuleContext` helpers:

- `ctx.subscribe(topic)`
- `ctx.emit(topic, payload)`
- `ctx.create_task(coro, name=...)`

This ensures the plugin permission model is enforced.

## Permission model for plugins

Plugin manifests define:

- `permissions.subscribe`
- `permissions.emit`

If a plugin tries to subscribe to or emit a topic that is not allowed, Lyndrix blocks the action and logs a warning.

## Recommended event usage

Good plugin and component behavior usually means:

1. keep topic names stable and namespaced, for example `domain:event_name`
2. keep payloads small and JSON-friendly
3. use events for cross-component contracts, not for hidden internal state
4. use tracked tasks for long-running async side effects
5. avoid placing secrets in event payloads

## Examples of common topics

- `system:started`
- `vault:ready_for_data`
- `db:connected`
- `system:boot_complete`
- `plugin:installed`
- `git:status_update`

For the full list currently used in the repository, see [events.md](events.md).
