# Database Component

## Purpose

The Database component manages the SQLAlchemy engine lifecycle and keeps Lyndrix connected to MariaDB with retry and watchdog behavior.

## Main location

- `app/core/components/database/logic/db_service.py`

## Responsibilities

- wait for Vault readiness via `vault:opened`
- build the SQLAlchemy engine and session factory
- retry connection attempts until the database becomes available or retries are exhausted
- distinguish transient failures from permanent configuration errors
- emit `db:connected` when the database is ready
- toggle maintenance mode when startup cannot continue safely
- run a watchdog that reconnects after connection loss

## Events

### Subscribes

- `vault:opened`

### Emits

- `db:connected`
- `system:maintenance_mode`

## Runtime behavior

At initialization time, the component:

1. builds the engine from `settings.DATABASE_URL`
2. starts an async connection loop
3. tests connectivity with `SELECT 1`
4. emits `db:connected` on success
5. starts a background watchdog

If the connection later fails, the watchdog clears the connected state and starts the reconnect loop again.

## Implementation notes

- SQLAlchemy runs with `pool_pre_ping=True`
- DB connect timeout is set through `connect_args`
- configuration-style failures stop retries early
- connection-style errors are sanitized before logging
