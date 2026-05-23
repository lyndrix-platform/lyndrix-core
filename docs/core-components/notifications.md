# Notifications Component

## Purpose

The Notifications component handles in-app notifications, persistence, toast delivery, and webhook-based ingestion.

## Main locations

- `app/core/components/notifications/entrypoint.py`
- `app/core/components/notifications/notification_service.py`
- `app/core/components/notifications/api.py`

## Responsibilities

- receive broadcast and user-targeted notifications from the event bus
- persist notification history
- display toast-style notifications to active UI clients
- expose a webhook API for external systems
- emit a normalized outbound notification event for integrations

## Events

### Subscribes

- `system:notify`
- `user:notify`

### Emits

- `notification:outbound`

## API endpoints

- `GET /api/notifications/webhook/gitlab/health`
- `POST /api/notifications/webhook/gitlab`

## Integration notes

This component is the preferred bridge between internal Lyndrix events and user-visible operational messages.
