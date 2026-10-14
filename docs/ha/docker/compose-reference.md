# Docker — Compose reference

## Core instance (Tier 1)

One service per host; DB, Vault and the load balancer are external to this stack.

```yaml
services:
  lyndrix:
    image: ghcr.io/lyndrix-platform/lyndrix-core:1.4.0
    restart: unless-stopped
    ports: ["8081:8081"]              # only reachable by the load balancer
    environment:
      ENV_TYPE: prod
      LYNDRIX_CLUSTER_MODE: cluster
      LYNDRIX_INSTANCE_ID: core-a     # unique per host
      LYNDRIX_INSTANCE_ROLES: api,leader          # add ",runner" on the host with the Docker socket
      LYNDRIX_UI_ENGINE: react
      LYNDRIX_TRUSTED_PROXIES: 10.0.0.10          # the LB / VIP
      LYNDRIX_DRAIN_SECONDS: "10"
      DB_HOST: 10.0.0.20              # writer endpoint (MaxScale) or the single MariaDB
      DB_NAME: lyndrix_db
      DB_USER: lyndrix
      DB_PASSWORD: ${DB_PASSWORD}
      VAULT_URL: http://10.0.0.30:8200
      VAULT_NODES: http://10.0.0.31:8200,http://10.0.0.32:8200,http://10.0.0.33:8200   # Tier 2
      LYNDRIX_MASTER_KEY: ${LYNDRIX_MASTER_KEY}
      STORAGE_SECRET: ${STORAGE_SECRET}           # identical on every host
      LYNDRIX_ADMIN_PASSWORD: ${LYNDRIX_ADMIN_PASSWORD}
      LYNDRIX_BOT_PASSWORD: ${LYNDRIX_BOT_PASSWORD}
    volumes:
      - /srv/lyndrix/security:/data/security      # vault_keys.enc — same file on every host
      - /srv/lyndrix/storage:/data/storage        # shared mount (NFS/CIFS) or local, see storage page
      - /srv/lyndrix/plugins:/app/plugins         # local; instances reconcile from the DB
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/api/ready', timeout=3).status == 200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 90s
    stop_grace_period: 30s           # ≥ LYNDRIX_DRAIN_SECONDS + uvicorn graceful shutdown
```

Runner host: add `- /var/run/docker.sock:/var/run/docker.sock` and the `HOST_*` paths the IaC
Orchestrator needs, and `LYNDRIX_INSTANCE_ROLES: api,leader,runner`.

## Frontend

```yaml
  lyndrix-ui:
    image: ghcr.io/lyndrix-platform/lyndrix-ui:1.4.0
    environment:
      LYNDRIX_BACKEND_URL: http://10.0.0.10        # the LB / VIP, never one instance
```

## The dev test bench

`docker/docker-compose.ha.dev.yml` is an overlay for the dev stack:

```bash
docker compose -f docker/docker-compose.dev.yml -f docker/docker-compose.ha.dev.yml up -d
curl http://localhost:8090/api/live        # alternates dev-a / dev-b
curl http://localhost:8081/api/ready       # instance A directly
curl http://localhost:8082/api/ready       # instance B directly
```

It gives both cores `LYNDRIX_CLUSTER_MODE=cluster`, the `db` coordinator, `LYNDRIX_DRAIN_SECONDS=5`
and the same plugin, storage and security mounts, and adds the Traefik entrypoint `ha` (`:8090`)
with the health check on `/api/ready` and `flushInterval: -1` for SSE. Instance A keeps the
`runner` role (IaC Orchestrator), B is `api,leader` and shows the orchestrator as *remote*.

Point the React dev shell at `http://traefik:8090` (container network) to drive the whole UI
through the balancer.

## Notes

- `stop_grace_period` must exceed the drain plus uvicorn's `--timeout-graceful-shutdown`,
  otherwise Docker kills the container mid-drain.
- `start_period` covers the boot (Vault → DB → IAM → modules); `/api/ready` is 503 until then.
- Both dev instances write `/app/logs/lyndrix.log` into the same mounted directory — harmless for
  development, never do that in production (use stdout).
