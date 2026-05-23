# Settings Component

## Purpose

The Settings component provides the central configuration UI page.

## Main locations

- `app/core/components/settings/ui/routes.py`
- `app/core/components/settings/ui/settings_ui.py`

## Responsibilities

- Register and render settings UI
- Expose runtime configuration forms for operators

## Events

The Settings component does not directly publish or subscribe to Event Bus topics in its route layer.

## UI endpoint

- `/settings`
