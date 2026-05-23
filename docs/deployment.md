# Installation and Deployment

This page explains how Lyndrix Core is started in local development, what is included in the repository, and what to keep in mind when deriving a production setup.

## Deployment model

The repository ships with a complete **development runtime**. It is the fastest way to boot the full stack locally and the best reference point for understanding required services.

The development stack includes:

- `docker/docker-compose.dev.yml`
- `docker/Dockerfile`
- `docker/.env.dev`
- `docker/entrypoint.sh`

These files start:

- the Lyndrix application container
- a MariaDB database
- a HashiCorp Vault instance
- a documentation preview server

## Local development setup

### Prerequisites

- Docker
- Docker Compose

### Start the stack

```bash
git clone https://github.com/lyndrix-platform/lyndrix-core.git
cd lyndrix-core
docker compose -f docker/docker-compose.dev.yml up -d --build
```

### Default local endpoints

- App: `http://localhost:8081`
- Vault UI: `http://localhost:8200`
- Docs preview: `http://localhost:8000`

## What the development stack does

The development compose file is optimized for fast iteration:

- the app runs with a reload-friendly development workflow
- repository files are mounted into the container
- local persistence is stored under `.dev/`
- the docs container serves the content from `docs/` through Zensical

### Important persistent paths

The development setup stores state in local bind mounts under `.dev/`.

Typical paths are:

- `../.dev/storage:/data/storage`
- `../.dev/secure_data:/data/security`
- `../.dev/db_data:/var/lib/mysql`
- `../.dev/vault_data:/vault/file`

These folders are important when you want to inspect local state, reset a broken development environment, or preserve data between restarts.

## Environment and bootstrap configuration

Important runtime settings are defined in `app/config.py` and can be supplied through environment variables.

Frequently used values include:

- `PORT`
- `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`
- `VAULT_URL`, `LYNDRIX_MASTER_KEY`
- `LYNDRIX_ADMIN_USER`, `LYNDRIX_ADMIN_PASSWORD`
- `LYNDRIX_PLUGINS_DESIRED`, `LYNDRIX_PLUGINS_AUTO_UPDATE`
- `LYNDRIX_AUTH_PROVIDERS`

For development, the defaults are intentionally convenient. For any shared or production-like environment, replace the default secrets and credentials with secure values.

## Plugin-related mounts in development

The development compose file supports plugin iteration and can be extended with additional bind mounts for external plugin repositories.

That is useful when:

- you develop Lyndrix Core and one or more plugins side by side
- you want a plugin to update immediately without packaging it first
- you need to inspect generated plugin dependencies in `vendor/`

If you add or change plugin mounts, ensure that the target host paths actually exist.

## Deriving a production setup

The repository does not ship a complete production deployment manifest. Instead, treat the development compose file as a reference implementation and derive your production setup from it.

A production-ready deployment should usually include:

- a pinned application image instead of a local build
- TLS termination through a reverse proxy or ingress
- dedicated persistent storage for the database, Vault, and platform data
- managed secrets instead of plain environment defaults
- monitoring, backup, and restore procedures
- explicit network boundaries between public, application, and data services

## Operational checks

Useful local checks:

```bash
# Container status
docker compose -f docker/docker-compose.dev.yml ps

# Application logs
docker compose -f docker/docker-compose.dev.yml logs -f lyndrix

# Basic health check
curl -f http://localhost:8081/
```

## Update guidance

Before any upgrade, create backups for:

- MariaDB data
- Vault storage
- `vault_keys.enc` from `settings.LYNDRIX_VAULT_KEY_FILE`

Recommended rollout order:

1. Create backups
2. Deploy the new application version
3. Watch the logs for boot events such as `system:boot_phase`
4. Verify plugin status in the plugin manager UI
5. Confirm that Vault, database, and login flows are healthy

## Troubleshooting

### The app never becomes ready

Check these first:

- whether Vault is initialized or still sealed
- whether the database container is reachable
- whether invalid credentials keep the DB in maintenance mode

### Login works but plugins do not load

Check these next:

- the logs around `iam:ready` and `system:boot_complete`
- plugin state rows in the database
- missing plugin dependencies or blocked dependency chains

### Docs preview is unavailable

The docs service depends on the repository content and the documentation build toolchain. Rebuild the compose stack and inspect the `docs` container logs.
