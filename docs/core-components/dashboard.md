# Dashboard Component

## Purpose

The Dashboard component owns the main authenticated landing page and provides a place for core and plugin widgets to render operational information.

## Main locations

- `app/core/components/dashboard/entrypoint.py`
- `app/core/components/dashboard/ui/routes.py`
- `app/core/components/dashboard/ui/dashboard_ui.py`

## Responsibilities

- register the dashboard page route
- render the default dashboard layout
- display runtime information to authenticated users
- host plugin-provided dashboard widgets through the plugin lifecycle hooks

## Events

The Dashboard component does not currently act as a major event producer. It mainly consumes shared runtime state exposed by the rest of the system.

## UI endpoint

- `/dashboard`

## Integration notes

Plugins can extend the dashboard by implementing `render_dashboard_widget(ctx)`. That keeps the dashboard extensible without requiring direct edits to the dashboard component itself.
