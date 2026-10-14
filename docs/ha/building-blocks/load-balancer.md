# Building block: load balancer

On Docker the load balancer is a **separate component you choose**; on Kubernetes the ingress
controller plays this role. Either way it has to meet the contract below — the core does not care
which product it is.

## Contract

1. **Health check `GET /api/ready`**: 200 means send traffic, anything else means take the
   instance out. Interval ≤ 5 s, two failures to remove, two successes to re-add. Never use
   `/api/health` as the probe — it calls every plugin's `health()`.
2. **Server-Sent Events**: no response buffering and a read/tunnel timeout of at least one hour
   for `/api/events` and `/api/plugins/*/stream/*`.
3. **Forwarded headers**: set `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`; on the
   core set `LYNDRIX_TRUSTED_PROXIES` to the balancer's address(es).
4. **One public origin** for UI and API (TLS termination at the balancer or passthrough).
5. **WebSocket upgrade** only if the legacy NiceGUI UI is enabled — then sticky cookies are
   mandatory (unsupported, but possible).
6. Treat a `503` from a plugin route (runtime gate, remote placement) as an application answer,
   not as "backend dead": keep health and request errors apart.
7. The balancer itself must not be the new single point of failure: a VIP (keepalived / CARP) or
   two balancers with DNS failover.

## Options

| Option | Active health checks | TLS | SSE | Sticky | LB HA | When |
|---|---|---|---|---|---|---|
| **Traefik** (file provider, static servers) | yes | ACME | `flushInterval: -1` | cookie | keepalived | you run Traefik already; do **not** rely on Docker discovery when the backends live on other hosts |
| **HAProxy** | yes (`option httpchk`) | yes | `timeout tunnel 1h` | cookie/source | keepalived or **pfSense package + CARP** | most precise health semantics; appliance variant without an extra host |
| **Caddy** | yes (`health_uri`) | automatic | `flush_interval -1` | cookie | keepalived | smallest configuration |
| **nginx OSS** | passive only (`max_fails`) | yes | `proxy_buffering off` | `ip_hash`/cookie | keepalived | already present; active checks need nginx Plus |
| Docker Swarm routing mesh | container healthcheck | — | yes | no | built in | if you run Swarm; put the health check in the image |
| DNS round robin | no | — | — | no | — | **not recommended** (browser failover is slow) |
| Hardware / cloud LB | yes | yes | usually | yes | yes | check the contract above |

## Lyndrix settings

`LYNDRIX_TRUSTED_PROXIES`, `LYNDRIX_DRAIN_SECONDS` (≥ two health-check intervals),
`LYNDRIX_UI_ENGINE=react`.

## Reference setups

Minimal configurations that satisfy the contract. Replace `10.0.0.11/12` with your core
instances.

### Traefik (file provider)

```yaml
# dynamic.yml
http:
  routers:
    lyndrix:
      rule: "Host(`lyndrix.example.com`)"
      entryPoints: [websecure]
      tls: { certResolver: letsencrypt }
      service: lyndrix
  services:
    lyndrix:
      loadBalancer:
        servers:
          - url: "http://10.0.0.11:8081"
          - url: "http://10.0.0.12:8081"
        healthCheck: { path: /api/ready, interval: 3s, timeout: 2s }
        responseForwarding: { flushInterval: "-1" }   # SSE
        passHostHeader: true
```

### HAProxy

```
frontend fe_https
    bind :443 ssl crt /etc/haproxy/certs/lyndrix.pem
    option forwardfor
    http-request set-header X-Forwarded-Proto https
    default_backend be_lyndrix

backend be_lyndrix
    option httpchk GET /api/ready
    http-check expect status 200
    default-server inter 3s fall 2 rise 2
    timeout tunnel 1h          # SSE
    server core-a 10.0.0.11:8081 check
    server core-b 10.0.0.12:8081 check
```

### Caddy

```
lyndrix.example.com {
    reverse_proxy 10.0.0.11:8081 10.0.0.12:8081 {
        health_uri /api/ready
        health_interval 3s
        flush_interval -1
    }
}
```

### nginx OSS (passive checks only)

```
upstream lyndrix {
    server 10.0.0.11:8081 max_fails=2 fail_timeout=5s;
    server 10.0.0.12:8081 max_fails=2 fail_timeout=5s;
}
server {
    listen 443 ssl;
    location / {
        proxy_pass http://lyndrix;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;          # SSE
        proxy_read_timeout 3600s;
    }
}
```

### keepalived (VIP for the balancer pair)

```
vrrp_script chk_lb { script "curl -sf -o /dev/null http://127.0.0.1:8404/healthz"; interval 2; fall 2; rise 2; }
vrrp_instance LYNDRIX_LB {
    state BACKUP; interface eth0; virtual_router_id 51; priority 100; advert_int 1; nopreempt
    authentication { auth_type PASS; auth_pass changeme }
    virtual_ipaddress { 10.0.0.10/24 }
    track_script { chk_lb }
}
```

### pfSense HAProxy package

Backend with the two servers, health check *HTTP*, URI `/api/ready`; frontend on the CARP VIP
with TLS offloading; set *Timeout tunnel* to 3600000 ms for SSE. The CARP VIP gives you the LB HA
without an extra host.

## Pinned plugins

A plugin pinned to a role (today: the IaC Orchestrator → `runner`) answers 503 "remote" on the
other instances. Add a routing rule for its paths to the runner instance, e.g. Traefik:

```yaml
    lyndrix-runner:
      rule: "Host(`lyndrix.example.com`) && PathPrefix(`/api/plugins/lyndrix.plugin.iac_orchestrator`)"
      service: lyndrix-runner      # one server: the runner instance
```

## Verify

```bash
curl -i https://lyndrix.example.com/api/ready          # 200, instance_id rotates between calls
curl -N https://lyndrix.example.com/api/events          # stream stays open > 60 s, keep-alives arrive
docker stop <one core>                                  # no 5xx at the balancer; /api/ready removes it within 2 checks
```

The dev test bench (`docker/docker-compose.ha.dev.yml`) runs exactly this with Traefik on
`:8090`.

## Known limits

- No reference `lyndrix-deploy` repository with these files yet; copy from this page.
- `pinned` placement is 503-based; transparent proxying to the pinned instance is not implemented.
