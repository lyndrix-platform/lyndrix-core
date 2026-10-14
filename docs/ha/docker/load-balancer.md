# Docker — the separate load balancer

On Docker nothing routes traffic for you; a load balancer in front of the core instances is a
required component of Tier 1. The product is your choice — the contract and the reference
configurations are on the building-block page:

- [Building block: load balancer](../building-blocks/load-balancer.md) — contract, comparison of
  Traefik / HAProxy / Caddy / nginx / Swarm / pfSense, minimal configurations, keepalived VIP,
  the routing rule for pinned plugins, verification.

## Docker specifics

- **Do not use Docker label discovery** for the core servers when they live on other hosts; use
  the file provider (Traefik), a static backend (HAProxy, Caddy, nginx) or a DNS name per host.
- The balancer's health check hits `/api/ready` on every core host over the network — open the
  core port (8081) to the balancer only.
- If the balancer runs on the app hosts (co-located layout), keepalived moves the VIP; the
  Traefik/HAProxy config is identical on every host.
- The dev test bench uses the dev Traefik's **Docker provider** with identical labels on both
  core containers — Traefik merges them into one service with two servers. That works because
  both containers are on the same host and network; it is a convenience for development, not the
  production pattern.

## Where to find the dev bench

`docker/docker-compose.ha.dev.yml` publishes the Traefik entrypoint `ha` on `:8090`; `curl
http://localhost:8090/api/live` alternates between `dev-a` and `dev-b`.
