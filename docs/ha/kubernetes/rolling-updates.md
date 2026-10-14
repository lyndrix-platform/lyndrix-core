# Kubernetes — rolling updates

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }
terminationGracePeriodSeconds: 30    # ≥ LYNDRIX_DRAIN_SECONDS + uvicorn graceful shutdown
```

With `maxUnavailable: 0` a new pod must be ready (`/api/ready` → 200) before an old one is
terminated. On termination the pod receives SIGTERM, drains (`/api/ready` → 503, the endpoint is
removed), closes SSE streams and releases its leases — the same sequence as on Docker.

Update the runner Deployment first, then the core Deployment:

```bash
kubectl set image deployment/lyndrix-runner lyndrix=ghcr.io/lyndrix-platform/lyndrix-core:1.4.0
kubectl rollout status deployment/lyndrix-runner
kubectl set image deployment/lyndrix-core lyndrix=ghcr.io/lyndrix-platform/lyndrix-core:1.4.0
kubectl rollout status deployment/lyndrix-core
```

Schema changes are additive and applied by the first new pod under `GET_LOCK('lyndrix_bootstrap')`.
A dedicated migration `Job` (`LYNDRIX_MIGRATE_MODE=job`) is planned but not required.

Rollback: `kubectl rollout undo deployment/lyndrix-core` — an older core runs against a newer,
additive schema.
