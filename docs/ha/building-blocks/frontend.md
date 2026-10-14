# Building block: frontend

`lyndrix-ui` is nginx with static files and a same-origin reverse proxy to the core
(`LYNDRIX_BACKEND_URL`). It has no state and scales to any number of replicas today.

## Contract

- `LYNDRIX_BACKEND_URL` points at the **load balancer / Service**, never at one core instance.
- The ingress health check for the frontend is `GET /` (nginx); core readiness is probed
  separately on `/api/ready`.
- SSE passes through nginx without buffering (`proxy_buffering off`, `proxy_read_timeout 3600s` —
  already in `docker/lyndrix-proxy.conf`).
- Plugin React bundles come from the core (`/api/plugins/<id>/static/`) → identical plugin
  artifacts on every core instance.

## Options

| Option | Notes |
|---|---|
| N `lyndrix-ui` containers behind the same balancer | stateless; the shell reconnects its SSE stream by itself after an `event: reconnect` |
| React served by the core (`LYNDRIX_UI_ENGINE=react`, `/app`) | one container type less; the core instances serve the SPA |
| Legacy NiceGUI | process-bound websocket; needs sticky sessions — **unsupported** in cluster mode (preflight warns) |

## What the shell does in a cluster

- The boot screen shows the cluster rows (coordinator, leader, peers, storage) from `GET /api/boot`.
- Runtime-status broadcasts carry `instance_id`; the warm-up panel shows whichever instance is
  warming up.
- A plugin pinned elsewhere renders a "runs on another instance" panel instead of a broken UI.
- Settings → Cluster shows instances, leader, leases, placements and the preflight, refreshed
  over SSE (`cluster:changed`).
- The Android app (TWA) points at the public hostname → the VIP.

## Known limits

- The shell has no per-instance view of plugin runtime status (it shows the latest broadcast).
