# Kubernetes — reference manifests (Tier 1)

Plain manifests for a Kustomize directory, verified on a k3s 1.30 cluster with Traefik
(CRD provider), cert-manager, MetalLB and Longhorn. Two core API replicas, two UI pods, one
MariaDB and one Vault in the cluster; the database and secrets tiers stay single (Tier 1).
Replace `lyndrix.example.com`, the storage class and the issuer with yours.

```
lyndrix/
├── kustomization.yaml   namespace, pvc, mariadb, vault, core, ui, certificate, ingressroute
├── secrets.yaml         NOT in git — see secrets.example.yaml below
└── …
```

## Secret

```yaml
apiVersion: v1
kind: Secret
metadata: { name: lyndrix-secrets, namespace: lyndrix }
type: Opaque
stringData:
  STORAGE_SECRET: <random>              # identical for every core pod — one Secret does that
  LYNDRIX_MASTER_KEY: <random, ≥ 32 chars>
  DB_PASSWORD: <random>
  MARIADB_ROOT_PASSWORD: <random>
  LYNDRIX_ADMIN_PASSWORD: <random>
  LYNDRIX_BOT_PASSWORD: <random>
```

## Shared volume

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: lyndrix-data, namespace: lyndrix }
spec:
  accessModes: [ReadWriteMany]           # /data/storage + /data/security for every replica
  storageClassName: longhorn
  resources: { requests: { storage: 10Gi } }
```

## MariaDB (single, Tier 1)

A `StatefulSet` with one replica, image `mariadb:12.2.2`, `MARIADB_DATABASE=lyndrix_db`,
`MARIADB_USER`/`MARIADB_PASSWORD`/`MARIADB_ROOT_PASSWORD` from the Secret, a headless Service
`lyndrix-db` on 3306, readiness `mariadb-admin ping`, an RWO Longhorn volume. Tier 2 replaces it
with the mariadb-operator (Galera + MaxScale) — only `DB_HOST` changes.

## Vault (single, Tier 1)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: lyndrix-vault, namespace: lyndrix }
spec:
  replicas: 1
  strategy: { type: Recreate }
  selector: { matchLabels: { app: lyndrix-vault } }
  template:
    metadata: { labels: { app: lyndrix-vault } }
    spec:
      securityContext: { fsGroup: 1000 }          # the image runs as uid 100 / gid 1000
      containers:
        - name: vault
          image: hashicorp/vault:2.0.3
          args: ["server"]
          env:
            - name: VAULT_LOCAL_CONFIG
              value: '{"storage": {"file": {"path": "/vault/file"}}, "listener": {"tcp": {"address": "0.0.0.0:8200", "tls_disable": 1}}, "ui": true, "disable_mlock": true}'
            - { name: VAULT_API_ADDR, value: http://lyndrix-vault:8200 }
          readinessProbe:
            httpGet: { path: "/v1/sys/health?standbyok=true&uninitcode=200&sealedcode=200", port: 8200 }
          volumeMounts: [{ name: data, mountPath: /vault/file }]
      volumes: [{ name: data, persistentVolumeClaim: { claimName: lyndrix-vault-data } }]
```

`disable_mlock` is required: the container is non-root and `mlock` would need an ambient
`IPC_LOCK`; the official Vault Helm chart disables it on Kubernetes as well. The core initialises
the Vault on first boot (`LYNDRIX_MASTER_KEY`) and writes the encrypted key file to the shared
`/data/security`, so every replica can unseal.

## Core

```yaml
apiVersion: v1
kind: ConfigMap
metadata: { name: lyndrix-core-config, namespace: lyndrix }
data:
  ENV_TYPE: "prod"
  LYNDRIX_CLUSTER_MODE: "cluster"
  LYNDRIX_INSTANCE_ROLES: "api,leader"      # no "runner" — the IaC Orchestrator needs a Docker host
  LYNDRIX_UI_ENGINE: "api"                  # React is served by the lyndrix-ui pods
  LYNDRIX_TRUSTED_PROXIES: "*"
  LYNDRIX_DRAIN_SECONDS: "10"
  DB_HOST: "lyndrix-db"
  DB_NAME: "lyndrix_db"
  DB_USER: "lyndrix"
  VAULT_URL: "http://lyndrix-vault:8200"
  CORS_ALLOWED_ORIGINS: '["https://lyndrix.example.com"]'
  LYNDRIX_PLUGINS_DESIRED: "https://github.com/lyndrix-platform/lyndrix-plugin-monitoring@v1.4.0,https://github.com/lyndrix-platform/lyndrix-plugin-server-manager@v1.2.0"
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: lyndrix-core, namespace: lyndrix, labels: { app: lyndrix-core } }
spec:
  replicas: 1                                # → 2 after the first boot, see below
  strategy: { type: RollingUpdate, rollingUpdate: { maxSurge: 1, maxUnavailable: 0 } }
  selector: { matchLabels: { app: lyndrix-core } }
  template:
    metadata: { labels: { app: lyndrix-core } }
    spec:
      terminationGracePeriodSeconds: 30      # ≥ drain + uvicorn graceful shutdown
      topologySpreadConstraints:
        - { maxSkew: 1, topologyKey: kubernetes.io/hostname, whenUnsatisfiable: ScheduleAnyway,
            labelSelector: { matchLabels: { app: lyndrix-core } } }
      containers:
        - name: lyndrix
          image: ghcr.io/lyndrix-platform/lyndrix-core:1.4.0
          ports: [{ containerPort: 8081 }]
          envFrom:
            - configMapRef: { name: lyndrix-core-config }
            - secretRef: { name: lyndrix-secrets }
          env:
            - name: LYNDRIX_INSTANCE_ID
              valueFrom: { fieldRef: { fieldPath: metadata.name } }
          securityContext: { capabilities: { add: [NET_RAW] } }   # ICMP probes (monitoring)
          startupProbe:   { httpGet: { path: /api/live,  port: 8081 }, periodSeconds: 5, failureThreshold: 60 }
          readinessProbe: { httpGet: { path: /api/ready, port: 8081 }, periodSeconds: 5, failureThreshold: 2 }
          livenessProbe:  { httpGet: { path: /api/live,  port: 8081 }, periodSeconds: 10, failureThreshold: 3 }
          volumeMounts:
            - { name: data, mountPath: /data/storage,  subPath: storage }
            - { name: data, mountPath: /data/security, subPath: security }
            - { name: plugins, mountPath: /app/plugins }   # per pod: plugins are pulled by tag at boot
            - { name: logs, mountPath: /app/logs }         # per pod: never share the log file
      volumes:
        - { name: data, persistentVolumeClaim: { claimName: lyndrix-data } }
        - { name: plugins, emptyDir: {} }
        - { name: logs, emptyDir: {} }
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: lyndrix-core, namespace: lyndrix }
spec: { minAvailable: 1, selector: { matchLabels: { app: lyndrix-core } } }
```

Service `lyndrix-core` (ClusterIP, 8081) selects `app: lyndrix-core`.

**First boot with one replica.** Two fresh pods would both see an uninitialised Vault and race
the `vault operator init`; the loser stays at *needs init*. Leave `replicas` out of the manifest
(the first apply creates one pod), wait for `/api/ready`, then
`kubectl scale deploy/lyndrix-core --replicas=2` — later `kubectl apply` calls keep that scale
instead of resetting it. Every later rollout can run with N replicas — database bootstrap and
the cluster tables are created under a cross-instance lock.

## UI

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: lyndrix-ui, namespace: lyndrix, labels: { app: lyndrix-ui } }
spec:
  replicas: 2
  selector: { matchLabels: { app: lyndrix-ui } }
  template:
    metadata: { labels: { app: lyndrix-ui } }
    spec:
      containers:
        - name: nginx
          image: ghcr.io/lyndrix-platform/lyndrix-ui:1.4.0
          env:
            - { name: LYNDRIX_BACKEND_URL, value: http://lyndrix-core.lyndrix.svc.cluster.local:8081 }   # FQDN — nginx's resolver ignores search domains
            - { name: LYNDRIX_DNS_RESOLVER, value: "10.43.0.10" }              # CoreDNS; the image defaults to Docker's 127.0.0.11
          readinessProbe: { httpGet: { path: /, port: 80 }, periodSeconds: 5 }
```

The nginx inside proxies `/api/*` and `/auth/callback/oidc` to the core Service with buffering
off (SSE), so the UI needs one public origin only. Two Kubernetes specifics: nginx resolves the
backend at runtime through `LYNDRIX_DNS_RESOLVER` (the image defaults to Docker's `127.0.0.11`
— set the kube-dns Service IP), and nginx's resolver ignores search domains, so
`LYNDRIX_BACKEND_URL` must carry the Service **FQDN**; a short name yields 502 "could not be
resolved".

## Certificate and route (Traefik CRD)

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: { name: lyndrix-prod-cert, namespace: lyndrix }
spec:
  secretName: lyndrix-tls-prod
  dnsNames: [lyndrix.example.com]
  issuerRef: { kind: ClusterIssuer, name: letsencrypt-production }
---
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: lyndrix
  namespace: lyndrix
  annotations: { kubernetes.io/ingress.class: traefik-external }   # whatever your Traefik's providers.kubernetescrd.ingressClass is
spec:
  entryPoints: [websecure]
  routes:
    - kind: Rule
      match: Host(`lyndrix.example.com`)
      services: [{ name: lyndrix-ui, port: 80 }]
  tls: { secretName: lyndrix-tls-prod }
```

With a plain `Ingress` controller use the manifest on the [ingress page](ingress.md) instead.

## Verify

```bash
kubectl -n lyndrix get pods -o wide                     # core pods on different nodes
kubectl -n lyndrix port-forward svc/lyndrix-core 8081   # then: curl -s localhost:8081/api/ready
kubectl -n lyndrix delete pod -l app=lyndrix-core --field-selector status.phase=Running --wait=false  # one at a time → the other keeps serving
```

Settings → Cluster shows both pods, the leader and the preflight; with the RWX volume the
`storage` check is `ok` ("shared mount — markers of 1 peer visible") and `redundancy` is `ok`
with two replicas.
