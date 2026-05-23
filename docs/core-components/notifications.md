# Notifications Component

## Purpose

The Notifications component handles in-app notifications, persistence, toast delivery, and webhook ingestion.

## Main locations

- `app/core/components/notifications/entrypoint.py`
- `app/core/components/notifications/notification_service.py`
- `app/core/components/notifications/api.py`

## Responsibilities

- Receive broadcast/user notification events
- Persist notification history
- Broadcast UI toasts to active clients
- Expose webhook API for external notification ingestion
- Emit normalized outbound notification event

## Events

### Subscribes

- `system:notify`
- `user:notify`

### Emits

- `notification:outbound`

## API endpoints

- `GET /api/notifications/webhook/gitlab/health`
- `POST /api/notifications/webhook/gitlab`
