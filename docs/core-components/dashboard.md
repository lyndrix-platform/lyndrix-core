# Dashboard Component

## Purpose

The Dashboard component provides the main post-login UI page for operational visibility.

## Main locations

- `app/core/components/dashboard/entrypoint.py`
- `app/core/components/dashboard/ui/routes.py`
- `app/core/components/dashboard/ui/dashboard_ui.py`

## Responsibilities

- Register dashboard page route
- Render dashboard widgets (including plugin-provided widgets)
- Present runtime status to authenticated users

## Events

The dashboard itself does not actively emit core bus topics. It consumes runtime state provided by other components and UI services.

## UI endpoint

- `/dashboard`
