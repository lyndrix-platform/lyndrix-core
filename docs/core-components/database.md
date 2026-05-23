# Database Component

## Purpose

The Database component manages SQLAlchemy engine lifecycle and resilient reconnect behavior for MariaDB.

## Main location

- `app/core/components/database/logic/db_service.py`

## Responsibilities

- Wait for Vault open state
- Create SQLAlchemy engine/session factory
- Retry connection with backoff and error classification
- Emit DB readiness and maintenance state
- Run background watchdog for connectivity health

## Events

### Subscribes

- `vault:opened`

### Emits

- `db:connected`
- `system:maintenance_mode`
