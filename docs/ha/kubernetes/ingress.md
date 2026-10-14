# Kubernetes — ingress and load balancing

The ingress controller fulfils the [load-balancer contract](../building-blocks/load-balancer.md):
readiness-driven membership comes from the Service (pods leave the endpoints when
`/api/ready` fails), TLS from cert-manager, forwarded headers from the controller.

## Traefik (k3s default)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: lyndrix
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
spec:
  ingressClassName: traefik
  tls: [{ hosts: [lyndrix.example.com], secretName: lyndrix-tls }]
  rules:
    - host: lyndrix.example.com
      http:
        paths:
          - path: /api/plugins/lyndrix.plugin.iac_orchestrator
            pathType: Prefix
            backend: { service: { name: lyndrix-runner, port: { number: 8081 } } }
          - path: /
            pathType: Prefix
            backend: { service: { name: lyndrix-core, port: { number: 8081 } } }
```

SSE through Traefik needs no buffering changes; set
`traefik.ingress.kubernetes.io/service.responseforwarding.flushinterval: "-1"` on the Service if
you see delayed events.

## ingress-nginx

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
```

## Core settings

`LYNDRIX_TRUSTED_PROXIES=*` (the ingress pod IPs change) or the pod CIDR;
`LYNDRIX_UI_ENGINE=react`.

## Verify

```bash
kubectl rollout restart deployment/lyndrix-core    # no 5xx at the ingress during the roll
kubectl get endpoints lyndrix-core                 # pods appear only when /api/ready is 200
```
