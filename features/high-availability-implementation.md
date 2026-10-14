# Umsetzungskonzept: HA modular — Docker **und** Kubernetes, Bausteine statt Blaupause

**Status:** 🚧 M0 + M1 umgesetzt (Core 1.4.0, Plugin-API 1.7.0, Branch `feature/ha-cluster`); M2–M6 offen
**Erstellt:** 2026-10-14, Fortsetzung von [`high-availability.md`](./high-availability.md) (Analyse + Zielarchitektur)

> **Umsetzungsstand 2026-10-14.** Geliefert (Core `feature/ha-cluster`, UI `feature/ha-cluster`,
> Monitoring `feature/ha-lease` v1.4.0, Orchestrator `feature/ha-placement` v1.3.0):
> `cluster`-Komponente mit Treibern `local`/`db` (Tabellen `cluster_*`, READ COMMITTED + Retry
> 1020/1205/1213, DB-Uhr), LeaseKeeper (Heartbeat, Fencing nach TTL, Release-Broadcast →
> Übernahme ≈ 1 s bei geordnetem Stop, ≤ TTL bei Kill), Bus-Bridge mit Allowlist + `emit(cluster=True)`
> + `_origin`, SSE-Relay und `event: reconnect` beim Drain, `/api/live` + `/api/ready` +
> `/api/cluster{,/preflight,/leases/{name}/release}`, Preflight (13 Prüfungen; nur `coordinator`
> und `storage_secret` nehmen die Instanz aus der Rotation), Drain auf SIGTERM, Cluster-Restart
> gestaffelt ohne Docker-Socket, Bootstrap-DDL/Seeds unter `GET_LOCK('lyndrix_bootstrap')`,
> DB-Reconnect endlos ohne Re-Bootstrap (`db:reconnected`, kein doppeltes `load_all`), OIDC-State/
> SSO-Codes/Login-Limiter im Cluster-KV, OIDC-Callback ohne NiceGUI, Settings-Propagation
> (`settings:changed`), Plugin-API `placement` + `ctx.on_lease/lease/is_leader/instance_id/cluster_size/roles`,
> `pinned` = 503 `remote` (Entscheidung §8.5: einfache Variante), Plugin-Artefakt-Reconcile aus
> `plugin_states` (`plugin:desired_changed` + 5-min-Pass), Boot-Screen-Zeilen echt, Cluster-Seite
> in der Shell (Settings → Cluster), englischer Doku-Baum `docs/ha/` (Entscheidung §8.2),
> Zwei-Instanzen-Prüfstand `docker/docker-compose.ha.dev.yml` (Traefik `:8090`, `/api/ready`-Check).
> Verifiziert auf dem Prüfstand: Leader-/Prober-Failover, Fan-out, LB-Round-Robin, Drain.
> **Nicht geliefert:** `lyndrix-deploy` (§5), Storage-Treiber (§3.2), Migrate-CLI (§3.3),
> `VAULT_NODES`-Unseal/AppRole, Redis-Treiber (§8.4: erst bei Bedarf), Helm-Chart, Galera/Raft-
> Referenzstacks, übrige Plugins aus M4 (docker-manager, bingo, discord, itsm-Anhänge, ssot-manager).
> Entscheidungen §8.1 (Repo), §8.3 (LB-Optionen in Doku: alle sieben aufgenommen), §8.6 (Cluster-
> Seite im Core: ja), §8.7 (Tier 1 offiziell: in `docs/ha/tiers.md` so ausgewiesen) — bitte bestätigen.
**Betroffene Repos:** `lyndrix-core` (Cluster-/Storage-Komponente, Preflight, Drain, Doku-Baum), `lyndrix-ui` (Cluster-Seite), alle Plugins (Placement/Leases), **neu** `lyndrix-deploy` (Compose-HA-Bundles, LB-Referenzkonfigs, Helm-Chart), `iac-environment` (eigene SSoTs)

> Vorgaben des Betreibers: HA muss **sowohl direkt auf Docker als auch auf k3s** aufgebaut werden
> können; für Docker braucht es einen **separaten Load Balancer**, der in der Anwendungs-Doku als
> Option hinterlegt wird; die Doku bekommt einen **komplett neuen HA-Baum**, weil nicht jeder HA
> will — wer will, soll **so modular und offen wie möglich** entscheiden können.
> Konvention: Prosa Deutsch, Code/Identifier/APIs Englisch.

---

## 0. Leitprinzipien

1. **Der Core kennt weder Docker noch Kubernetes.** Er kennt nur Umgebungsvariablen und
   *Verträge* (was ein Load Balancer, eine Datenbank, ein Vault, ein Storage leisten muss).
   Deployment-Profile sind Artefakte und Doku *außerhalb* des Codes. So funktioniert jede
   Core-Änderung automatisch auf beiden Plattformen.
2. **Opt-in, nie Opt-out.** `LYNDRIX_CLUSTER_MODE=single` ist Default und verhält sich exakt wie
   heute. Der Koordinator läuft dann mit dem Treiber `local` (Leader = immer ich, jede Lease wird
   gewährt, Events bleiben lokal). Plugins schreiben **einen** Code-Pfad, der in beiden Modi läuft.
3. **Bausteine statt Blaupause.** Jede Ebene (LB, Koordination, DB, Secrets, Storage, Plugins,
   Frontend) hat einen *Vertrag* und *N Optionen* mit Vergleich. Der Core **validiert** den
   Vertrag (Preflight) statt eine Option vorzuschreiben.
4. **Stufen, auf denen man stehen bleiben darf** (§1): robuste Einzelinstanz → App-HA →
   Daten-HA → DR. Jede Stufe ist für sich dokumentiert, ehrlich mit ihren verbleibenden SPOFs.
5. **Gleiche Operator-Erfahrung auf beiden Plattformen:** dieselben Env-Namen (Helm-Values
   mappen 1:1), dieselbe Cluster-Seite in der UI, dieselbe Preflight-Prüfung, dieselben Drills.

---

## 1. Stufenmodell (Tiers)

| Tier | Name | Was läuft N-fach | Verbleibende SPOFs | Mindestens nötig |
|---|---|---|---|---|
| **0** | Robuste Einzelinstanz | nichts | alles | Backups, `/api/ready`, Healthcheck, Restart-Policy, Drain |
| **1** | App-HA | Core-API, Frontend | DB, Vault, Storage | LB, Koordination (`db`), gleiches `STORAGE_SECRET`, Plugin-Artefakte identisch, Worker-Rolle für Orchestrator |
| **2** | Daten-HA | + DB (Galera, 1 Writer), Vault (Raft) | Storage (gnädig degradierend), Standort | MaxScale/Proxy, `VAULT_NODES`, Shared/Object-Storage |
| **3** | DR | Backup-Ziel am zweiten Standort, Restore-Runbook | — | Off-Site-Dumps/Snapshots, dokumentierter Wiederanlauf |

Tier 1 ist bewusst eigenständig: viele wollen Rolling Updates ohne Downtime und einen toten
Core-Container überleben, ohne gleich Galera zu betreiben. Die Doku sagt klar, was Tier 1 **nicht**
abdeckt.

---

## 2. Konfigurationsoberfläche — die eine Wahrheit für Compose, Helm und SSoT

Alle Namen folgen den bestehenden Präfixen (`LYNDRIX_*`, `DB_*`, `VAULT_*`). Helm-Values und
SSoT-Variablen sind nur Transportwege für genau diese Werte.

| Variable | Default | Tier | Bedeutung |
|---|---|---|---|
| `LYNDRIX_CLUSTER_MODE` | `single` | 1 | `single` \| `cluster`. `cluster` aktiviert Koordinator, Preflight-Pflicht, React-only, Drain |
| `LYNDRIX_INSTANCE_ID` | auto (`<hostname>-<rand>`) | 1 | stabile Kennung nur nötig für Diagnose; neue ID nach Restart ist harmlos (Leases laufen aus) |
| `LYNDRIX_INSTANCE_ROLES` | single: `api,leader,runner` · cluster: `api,leader` | 1 | `api` (Routen), `leader` (lease-fähig für Core-Einmalaufgaben + `singleton`-Plugins), `runner` (für `pinned:runner`-Plugins; braucht Docker-Socket) |
| `LYNDRIX_COORDINATOR` | single: `local` · cluster: `db` | 1 | `local` \| `db` \| `redis` |
| `LYNDRIX_REDIS_URL` | — | opt. | nur Treiber `redis` |
| `LYNDRIX_LEASE_TTL_SECONDS` / `LYNDRIX_LEASE_HEARTBEAT_SECONDS` | `30` / `10` | 1 | Lease-Semantik |
| `LYNDRIX_EVENT_POLL_MS` | `500` | 1 | Treiber `db`: Poll-Intervall `cluster_events` |
| `LYNDRIX_MIGRATE_MODE` | `auto` | 0 | `auto` (Boot unter `GET_LOCK`), `job` (Instanzen machen **kein** DDL, prüfen nur Schema-Version → Compose-Init-Service / k8s-Job führt `lyndrix migrate` aus), `off` |
| `LYNDRIX_PLUGIN_ARTIFACTS` | `local` | 1 | `local` (wie heute), `reconcile` (jede Instanz zieht `plugin_states.desired_version` selbst), `image` (im Image gebacken, Installs nur als DB-Wunsch + Hinweis „neues Image bauen“) |
| `LYNDRIX_STORAGE_DRIVER` | `local` | 1/2 | `local` \| `fs` (Shared-Mount, wird auf Beschreibbarkeit + Gemeinsamkeit geprüft) \| `s3` |
| `LYNDRIX_STORAGE_S3_ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY/REGION` | — | opt. | Treiber `s3` (MinIO, Garage, SeaweedFS, AWS) |
| `LYNDRIX_TRUSTED_PROXIES` | `127.0.0.1` | 0 | → uvicorn `--forwarded-allow-ips`; Pflicht hinter jedem LB (Rate-Limiter, Audit-IP) |
| `LYNDRIX_DRAIN_SECONDS` | single `0` · cluster `10` | 0/1 | SIGTERM → `/api/ready` 503 → warten → SSE schließen → Leases freigeben → Exit |
| `LYNDRIX_UI_ENGINE` | `react` | 1 | in `cluster` erzwungen `react`; `LYNDRIX_ALLOW_LEGACY_UI_IN_CLUSTER=1` nur mit Sticky-LB + Shared `/app/.nicegui` (dokumentiert als „nicht unterstützt, aber möglich“) |
| `DB_HOST` | — | 0 | **Writer-Endpunkt** (MaxScale/ProxySQL/Service). Nie ein Galera-Knoten direkt bei N Instanzen |
| `DB_POOL_RECYCLE_SECONDS` / `DB_RETRY_ATTEMPTS` | `300` / `3` | 1 | Failover-Toleranz (1213/1205/2006/2013) |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `60` | 0 | vorhanden (API 1.5) |
| `VAULT_URL` | — | 0 | LB- oder Active-Endpunkt |
| `VAULT_NODES` | — | 2 | Liste aller Knoten für Health-Poll + Unseal (`https://v1:8200,https://v2:8200,…`) |
| `VAULT_UNSEAL_MODE` | `lyndrix` | 2 | `lyndrix` (Leader entsiegelt alle `VAULT_NODES` mit dem Keyfile), `external` (Transit/KMS/Auto-Unseal auf Vault-Seite; Core pollt nur), `manual` |
| `VAULT_AUTH` | `root-file` | opt. | `root-file` (heute) \| `approle` (`VAULT_ROLE_ID`, `VAULT_SECRET_ID`, Renewal-Loop) |
| `LYNDRIX_MASTER_KEY` + `SECURITY_DIR/vault_keys.enc` | — | 0 | unverändert; in HA als Secret/Shared-Secret an jede Instanz |
| `STORAGE_SECRET` | — | 0 | unverändert; muss auf allen Instanzen identisch sein (Preflight prüft Hash in `cluster_instances`) |

**Preflight** (`GET /api/cluster/preflight`, auch `lyndrix doctor`) prüft im `cluster`-Modus und
verweigert den Start bei harten Verstößen (mit Klartext-Meldung, welcher Vertrag verletzt ist):
`STORAGE_SECRET` identisch; `DB_HOST` ist kein Galera-Knoten direkt (`@@wsrep_on` + `@@wsrep_node_name`
vs. Proxy-Erkennung) oder ausdrücklich `DB_ALLOW_DIRECT_NODE=1`; Schema-Version passt; `VAULT_NODES`
alle erreichbar; Storage-Treiber ist nicht `local`, wenn Plugins Anhänge speichern; mindestens eine
Instanz mit Rolle `runner`, wenn ein `pinned:runner`-Plugin aktiv ist; `LYNDRIX_TRUSTED_PROXIES`
gesetzt, wenn `X-Forwarded-For` ankommt; Uhrzeit-Drift zwischen Instanzen < 5 s (Leases!).

---

## 3. Core-Bausteine (Code)

### 3.1 Komponente `core/components/cluster/`

```
cluster/
├── logic/
│   ├── coordinator.py      # Protocol + Factory (LYNDRIX_COORDINATOR)
│   ├── drivers/local.py    # single: Leases immer gewährt, publish = no-op
│   ├── drivers/db.py       # Tabellen cluster_instances/leases/kv/events, Poll-Loop
│   ├── drivers/redis.py    # SET NX PX, Pub/Sub, INCR/EXPIRE (später)
│   ├── leases.py           # LeaseKeeper: Heartbeat, Callbacks acquired/lost, Jitter
│   ├── bridge.py           # Bus-Bridge: Allowlist-Topics ↔ publish/subscribe, _origin
│   ├── preflight.py        # Vertragsprüfungen (§2)
│   └── drain.py            # SIGTERM-Handling, ready=False, Leases freigeben
├── api/cluster_api.py      # GET /api/cluster, /preflight, /leases; POST /leases/{name}/release (admin)
└── ui/react/               # Cluster-Seite (in lyndrix-ui: Instanzen, Leader, Leases, Placements, Preflight)
```

```python
class Coordinator(Protocol):
    instance_id: str
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def try_acquire(self, name: str, ttl: int) -> bool: ...
    async def release(self, name: str) -> None: ...
    async def holder(self, name: str) -> str | None: ...
    async def kv_set(self, key: str, value: Any, ttl: int) -> None: ...
    async def kv_get(self, key: str) -> Any | None: ...
    async def kv_incr(self, key: str, ttl: int) -> int: ...          # Rate-Limiter
    async def publish(self, topic: str, payload: dict) -> None: ...
    def subscribe(self, callback: Callable[[str, dict], Awaitable[None]]) -> None: ...
    async def instances(self) -> list[InstanceInfo]: ...
```

Verbraucher im Core: Leader-Lease `core:leader` (Seeds im `auto`-Modus, Collection-Sync,
Update-Checker, Messaging-Outbox, Vault-Unseal-Poll, `singleton`-Plugin-Leases), OIDC-State +
SSO-Codes + Limiter → `kv_*`, Bridge für die Allowlist-Topics aus der Analyse (§9.4 dort),
SSE-Relay, `/api/health` → `/api/live` + `/api/ready` + tief gecacht.

### 3.2 Komponente `core/components/storage/`

`ObjectStore`-Protocol (`put/get/delete/exists/url/list`) mit Treibern `local`, `fs`, `s3`;
Export als `ctx.storage` (Namespace je Plugin: `plugins/<id>/…`). Umsteller: Theme-Uploads,
User-Backgrounds, ITSM-Anhänge, Plugin-Zip-Cache. Der Treiber `fs` ist technisch `local` plus
Preflight „ist das Verzeichnis auf allen Instanzen derselbe Mount?“ (Marker-Datei mit
`instance_id`, von den anderen gelesen).

### 3.3 Migration und Bootstrap

- CLI `python -m app.cli migrate` (Image-Entrypoint `entrypoint.sh migrate`): `create_all`, alle
  heute verstreuten `ALTER`s, Seeds — **idempotent**, unter `GET_LOCK('lyndrix_migrate', 120)`,
  schreibt `lyndrix_schema(component, version)`.
- Plugins ersetzen DDL in `setup()`/`health()` durch `ctx.migrate(fn)`: der Core führt `fn` im
  Migrationsjob bzw. auf dem Leader unter Lock aus; Instanzen ohne passende Schema-Version warten
  (`starting`, nicht blocking → Routen 503 mit Hinweis), statt selbst zu altern.
- Compose: `init`-Service `lyndrix-migrate` mit `depends_on: condition: service_completed_successfully`;
  k8s: `Job` als Helm-Hook `pre-install,pre-upgrade`.

### 3.4 Plugin-Artefakte

Treiber `reconcile`: Reconciler je Instanz vergleicht `plugin_states.desired_version` mit
`/app/plugins/<repo>/entrypoint.py`-Manifest-Version, lädt bei Abweichung (Zip aus Release-Tag,
`vendor` via pip), lädt Modul neu — ausgelöst durch Cluster-Event `plugin:desired_changed` und alle
`LYNDRIX_PLUGIN_RECONCILE_SECONDS` (300). Marketplace-Install im Cluster = nur DB-Wunsch +
Event; der Leader verifiziert vorab (Download + Health im Tempdir), damit nicht N Instanzen
gleichzeitig ein kaputtes Plugin ziehen. Treiber `image`: Reconciler meldet Abweichung nur
(`plugin:artifact_mismatch`), installiert nichts.

### 3.5 Drain und Rolling Update

SIGTERM → `ready=False` (`/api/ready` 503, LB nimmt die Instanz nach 1–2 Checks raus) → warten
`LYNDRIX_DRAIN_SECONDS` → SSE-Streams mit `event: reconnect` schließen → Leases freigeben
(Leader-Wechsel sofort statt nach TTL) → `stop()` der Plugins → Exit. uvicorn
`--timeout-graceful-shutdown` bleibt der Notnagel dahinter. k8s: `terminationGracePeriodSeconds`
≥ Drain + 15.

### 3.6 Plugin-API (Core API 1.6)

```python
manifest = ModuleManifest(..., placement="any")   # any | singleton | pinned:<role>

def setup(ctx):
    ctx.register_routes(router)                                  # jede api-Instanz
    ctx.on_lease("monitoring:prober", acquired=svc.start, lost=svc.stop)
    ctx.emit("monitoring:monitors_changed", {...}, cluster=True)
    ctx.migrate(svc.ensure_schema)                                # statt DDL in setup()
    store = ctx.storage                                          # ObjectStore, namespaced

async with ctx.lease("itsm:connector_poll") as held:
    if held: ...
ctx.is_leader, ctx.instance_id, ctx.cluster_size, ctx.roles
```

Kompatibilität wie bei `set_status`: `getattr(ctx, "on_lease", None)`; auf alten Cores fällt das
Plugin auf „immer aktiv“ zurück (= heutiges Verhalten).

### 3.7 UI (`lyndrix-ui`)

Seite **Settings → Cluster** (nur `cluster`-Modus sichtbar): Instanzen mit Rollen, Version,
Heartbeat, Leader-Marker; Leases mit Halter und Restlaufzeit; Plugin-Placements und wo sie laufen;
Preflight-Ergebnis mit Klartext; Button „Lease freigeben“ (admin). Runtime-Status-Pills zeigen
`instance_id`, wenn Instanzen abweichen.

---

## 4. Der separate Load Balancer für Docker

### 4.1 LB-Vertrag (plattformneutral, steht so in der Doku)

1. Health-Check **`GET /api/ready`** (200 = Verkehr, alles andere = raus), Intervall ≤ 5 s,
   `fall 2 / rise 2`. Nie `/api/health` als LB-Check (teuer, prüft Plugins).
2. **SSE**: kein Response-Buffering, Read-/Tunnel-Timeout ≥ 1 h für `/api/events` und
   `/api/plugins/*/stream/*`.
3. **Forwarded-Header** (`X-Forwarded-For/Proto/Host`) setzen; Core-Seite `LYNDRIX_TRUSTED_PROXIES`.
4. **Eine öffentliche Origin** für UI + API (TLS-Terminierung am LB oder Passthrough).
5. **WebSocket**-Upgrade nur, wenn Legacy-UI erlaubt ist; dann **Sticky-Cookie** Pflicht.
6. Optional: `Retry-After`/503 vom Runtime-Gate durchreichen (nicht als „Backend tot“ werten,
   d. h. Health-Check getrennt vom Request-Fehler).
7. Der LB **selbst** darf kein SPOF sein → VIP (keepalived/CARP) oder zwei LBs + DNS-Failover.

### 4.2 Optionen (Doku-Vergleichstabelle)

| Option | Aktive Health-Checks | TLS | SSE | Sticky | LB-HA | Wann |
|---|---|---|---|---|---|---|
| **Traefik** (File-Provider, statische Server) | ja (`healthCheck.path`) | ACME | ja (`responseForwarding.flushInterval`) | Cookie | mit keepalived | Standard im Fleet; Container-Discovery **nicht** nutzen (Backends liegen auf anderen Hosts) |
| **HAProxy** | ja (`option httpchk`) | ja | `timeout tunnel 1h` | Cookie/Source | keepalived oder **pfSense-Paket + CARP** | präziseste Health-Semantik; Appliance-Variante ohne eigenen Host |
| **Caddy** | ja (`health_uri`) | automatisch | `flush_interval -1` | Cookie | keepalived | kleinste Konfiguration, für Einsteiger |
| **nginx OSS** | **nur passiv** (`max_fails`) | ja | `proxy_buffering off` | `ip_hash`/Cookie | keepalived | wenn ohnehin vorhanden; aktive Checks nur in Plus |
| **Docker Swarm** Routing-Mesh | Container-Healthcheck | — | ja | nein | eingebaut | wenn jemand Swarm nutzt; kein Sticky, kein `/api/ready` → `HEALTHCHECK` im Image nötig |
| **DNS Round-Robin** | nein | — | — | nein | — | **nicht empfohlen**, nur Übergang (Browser-Failover langsam) |
| Hardware/Cloud-LB | ja | ja | meist | ja | ja | wenn vorhanden; Vertrag §4.1 prüfen |

### 4.3 Wo der LB läuft

- **Dediziertes Paar** (sauberste Trennung, zwei kleine LXCs) mit keepalived-VIP.
- **Kolokiert** auf jedem App-Host (3 Hosts → 3× LB + Core) mit einer VIP, die wandert; spart
  Hosts, koppelt aber LB- und App-Ausfall.
- **Appliance**: pfSense HAProxy-Paket + CARP-VIP — passt zur bestehenden pfSense, kein Container.
- **k3s**: nicht nötig — Ingress + MetalLB/kube-vip übernehmen den Vertrag; die Doku zeigt die
  Annotationen (Traefik `traefik-external`, Health über `readinessProbe`).

### 4.4 Ausgelieferte Referenzkonfigurationen (`lyndrix-deploy/docker/ha/lb/`)

`traefik/{traefik.yml,dynamic.yml}`, `haproxy/haproxy.cfg`, `caddy/Caddyfile`,
`nginx/lyndrix-lb.conf`, `keepalived/{keepalived.conf,check_lb.sh}`, `pfsense/README.md`
(Klick-Anleitung + Export). Jede Konfiguration implementiert §4.1 vollständig und ist mit einem
Smoke-Skript (`scripts/lb-contract-test.sh <url>`: ready-Check, SSE-Stream 70 s offen,
Forwarded-Header sichtbar in `/api/system/info`) prüfbar.

---

## 5. Deployment-Profile als Artefakte: neues Repo `lyndrix-deploy`

Begründung für ein eigenes Repo: eigene Release-Kadenz (Chart-Version ↔ Core-Image-Version),
ein Ort für alles Betreiber-Relevante, keine Vermischung mit dem Dev-Compose im Core. Alternative
(im Core unter `deploy/`) ist möglich, verliert aber die saubere Versionierung des Charts.

```
lyndrix-deploy/
├── docker/
│   ├── single/                 docker-compose.yml  (Tier 0: heutiges Produktiv-Setup + Healthcheck + Backups)
│   └── ha/
│       ├── core/               docker-compose.yml  (eine api-Instanz; DB/Vault/Storage extern via .env)
│       ├── runner/             docker-compose.yml  (Rolle runner, docker.sock, HOST_*-Pfade)
│       ├── lb/                 traefik | haproxy | caddy | nginx | keepalived | pfsense  (§4.4)
│       ├── db/                 galera-node/ (je Host) + maxscale/ + bootstrap.sh + backup.sh
│       ├── vault/              raft-node/ (vault **oder** openbao, gleiche Dateien) + join.sh
│       ├── storage/            minio/ (optional)
│       ├── migrate/            Init-Service-Snippet
│       └── .env.ha.example
├── kubernetes/
│   ├── helm/lyndrix/           Chart (values: core.replicas, core.roles, runner.enabled,
│   │                            database.mode external|operator|bundled, vault.mode external|raft|bundled,
│   │                            storage.mode, coordinator, ingress.className, migrate.job)
│   ├── kustomize/              base + overlays (k3s-traefik, ingress-nginx)
│   └── examples/               k3s-full-ha.values.yaml, external-db-vault.values.yaml, tier1.values.yaml
├── scripts/
│   ├── ha-preflight.sh         curl /api/cluster/preflight auf jeder Instanz
│   ├── ha-rolling-update.sh    Reihenfolge: migrate → runner → api-Instanzen einzeln, wartet auf /api/ready
│   ├── lb-contract-test.sh
│   └── failover-drill.sh       Instanz/DB-Knoten/Vault-Knoten stoppen, Messpunkte ausgeben
└── README.md → verweist auf den HA-Baum der Core-Doku
```

**Mapping Helm ↔ Env** (Auszug): `core.roles` → `LYNDRIX_INSTANCE_ROLES`; `coordinator` →
`LYNDRIX_COORDINATOR`; `database.external.host` → `DB_HOST`; `vault.nodes` → `VAULT_NODES`;
`storage.s3.*` → `LYNDRIX_STORAGE_S3_*`; `migrate.job=true` → `LYNDRIX_MIGRATE_MODE=job`.
Jede Helm-Option ist in der Doku mit ihrer Env-Variable gelistet, damit Compose- und k8s-Leser
dasselbe Vokabular haben.

**Marvins Fleet (iac-environment):** aus denselben Referenzdateien entstehen SSoTs
`aac-lyndrix-core` (Rolle `api`, je Host ein Eintrag), `aac-lyndrix-runner`, `aac-lyndrix-lb`,
`aac-lyndrix-db`, `aac-lyndrix-vault`; die heutigen `dependencies:` DB/Vault im Core-SSoT
entfallen. Die Engine-Eigenschaft „gleiche SSoT auf mehreren Hosts = unabhängige Stacks“ ist genau
das, was Galera-/Raft-Knoten brauchen.

---

## 6. Der neue Doku-Baum (`lyndrix-core/docs/ha/`)

**Sprache:** die MkDocs-Site (`docs/deployment.md` „Installation and Deployment“) ist englisch →
der HA-Baum wird **englisch**, damit die Site konsistent bleibt; `features/`-Konzepte bleiben
deutsch. (Entscheidung §8.)

**Grundsatz:** `deployment.md` bleibt der Weg für „ich will einfach Lyndrix“. Der HA-Baum ist ein
eigener Top-Level-Eintrag, beginnt mit einer **Entscheidungshilfe** und endet mit Drills — wer HA
nicht will, muss ihn nie öffnen.

```yaml
# mkdocs.yml (Ergänzung nav)
  - High Availability:
      - Overview & decision guide: ha/index.md          # Fragen: Hosts? k8s? Vault/DB vorhanden? Orchestrator? → Tier + Profil
      - Tiers: ha/tiers.md                              # Tier 0–3, was jede Stufe (nicht) abdeckt
      - Architecture & contracts: ha/architecture.md    # Ebenenbild, Leader/Leases/placement, Verträge
      - Configuration reference: ha/configuration.md    # Tabelle §2, Preflight-Regeln
      - Building blocks:
          - Load balancer: ha/building-blocks/load-balancer.md
          - Coordination: ha/building-blocks/coordination.md       # local | db | redis
          - Database: ha/building-blocks/database.md               # single | galera+maxscale | galera+proxysql | primary/replica | operator | managed
          - Secrets (Vault / OpenBao): ha/building-blocks/secrets.md   # single | raft | external; Unseal-Modi
          - Shared storage: ha/building-blocks/storage.md          # local | fs (NFS/CIFS/RWX) | s3
          - Plugin artifacts & placement: ha/building-blocks/plugins.md
          - Frontend: ha/building-blocks/frontend.md
      - Docker:
          - Overview & host layouts: ha/docker/index.md           # 3 Hosts; 2 + Arbiter; dediziertes LB-Paar
          - The separate load balancer: ha/docker/load-balancer.md  # §4 mit allen Referenzkonfigs
          - Galera + MaxScale: ha/docker/galera-maxscale.md
          - Vault / OpenBao Raft: ha/docker/vault-raft.md
          - Compose reference: ha/docker/compose-reference.md
          - Rolling updates: ha/docker/rolling-updates.md
      - Kubernetes:
          - Overview: ha/kubernetes/index.md
          - Helm chart: ha/kubernetes/helm.md
          - Database options: ha/kubernetes/database.md           # mariadb-operator, Bitnami, external
          - Vault options: ha/kubernetes/vault.md                 # Helm Raft, external, Unseal
          - Ingress & load balancing: ha/kubernetes/ingress.md
          - Rolling updates: ha/kubernetes/rolling-updates.md
      - Operations:
          - Cluster status & preflight: ha/operations/cluster-status.md
          - Backup & restore: ha/operations/backup-restore.md
          - Failover drills: ha/operations/failover-drills.md
          - Troubleshooting: ha/operations/troubleshooting.md
      - For plugin developers: ha/plugin-development.md           # placement, on_lease, cluster events, DDL-Regeln, PK-Pflicht
```

**Seitenschablone für jeden Baustein** (gleiches Raster, damit Vergleichen leicht ist):
1. *Contract* — was Lyndrix von dieser Ebene verlangt (kurz, prüfbar).
2. *Options* — Tabelle: Option · HA der Ebene selbst · Aufwand · Besonderheiten · wann wählen.
3. *Lyndrix settings* — die betroffenen Variablen aus §2.
4. *Reference setup* — Verweis auf `lyndrix-deploy/…` + Minimalbeispiel.
5. *Verify* — konkrete Kommandos (Preflight-Zeile, Drill).
6. *Known limits*.

Die Entscheidungshilfe (`ha/index.md`) ist ein kurzer Fragenbaum: *Wie viele Hosts? (1 → Tier 0;
2 → Tier 1 oder 2 + Arbiter; ≥ 3 → alles) · Kubernetes vorhanden? (→ Kubernetes-Zweig) · Bestehender
Vault/OpenBao? (→ `vault.mode external`) · Bestehende DB-HA? (→ `database.mode external`, Vertrag
prüfen) · IaC-Orchestrator im Einsatz? (→ Runner-Rolle einplanen) · Legacy-UI nötig? (→ Sticky-LB,
nicht unterstützt)* — jede Antwort verlinkt die passende Seite.

---

## 7. Umsetzungsreihenfolge mit Meilensteinen (beide Plattformen früh testbar)

| M | Ergebnis | Inhalt | Nachweis |
|---|---|---|---|
| **M0** | Tier 0 ist echt | `/api/live` + `/api/ready`, Drain, `LYNDRIX_TRUSTED_PROXIES`, `migrate`-CLI + `GET_LOCK`, Boot-Races idempotent, Vault-Health-Poll, Notifications → DB, Logs stdout, Backup-Skripte in `lyndrix-deploy/docker/single` | DB stoppen/starten ohne Re-Bootstrap; Restore aus Dump auf `docker-test` |
| **M1** | Core `cluster`-Modus (API 1.6) | `cluster`-Komponente mit `local`+`db`, Bridge, SSE-Relay, Leader-Aufgaben, kv für OIDC/Limiter, `reconcile`-Artefakte, Preflight, Cluster-API; **Dev-Compose mit 2 Instanzen hinter `docker/traefik.dev.yml`** als Pflicht-Prüfstand | Install auf A sichtbar auf B; Leader-Kill → Übernahme ≤ 30 s; OIDC mit Instanzwechsel |
| **M2** | Docker Tier 1 end-to-end | `lyndrix-deploy/docker/ha/{core,runner,lb}` + alle LB-Referenzkonfigs + `lb-contract-test.sh`; Doku `ha/docker/*` + `ha/building-blocks/load-balancer.md` | 2 Hosts + LB-Paar mit keepalived: Rolling Update ohne 5xx; Instanz killen unsichtbar |
| **M3** | k3s Tier 1 | Helm-Chart (`database.mode external`, `vault.mode external`, Migrate-Job, Probes, PDB, `terminationGracePeriodSeconds`), Kustomize-Overlay k3s-Traefik; Doku `ha/kubernetes/*` | Chart auf dem bestehenden k3s mit externer Test-DB/Vault; `kubectl rollout restart` ohne 5xx |
| **M4** | Plugins cluster-fähig | monitoring (Lease-Prober, UNIQUE + atomare Zähler), orchestrator (`pinned:runner`, `owner_instance`, Reservierung, Trigger-Events), ssot-manager (ablösen/pinnen), docker-manager/discord/bingo → DB, server-manager/registry/itsm `ctx.migrate` + Retry, itsm Anhänge → `ctx.storage`, external-services Sidebar aus DB | Heartbeat-Rate bei 2 Instanzen = bei 1; Orchestrator-Job überlebt API-Restart |
| **M5** | Tier 2 beide Plattformen | `docker/ha/{db,vault,storage}` (Galera+MaxScale, Raft, MinIO) + `VAULT_NODES`-Unseal + DB-Retry; Helm `database.mode operator` (mariadb-operator), `vault.mode raft`; Doku `galera-maxscale`, `vault-raft`, k8s `database`/`vault` | Writer killen → ≤ 1 Retry; Vault-Knoten sealen → Leader entsiegelt ≤ 20 s |
| **M6** | Doku-Baum vollständig + Drills | Entscheidungshilfe, Tiers, Operations, Plugin-Developer-Seite, `failover-drill.sh`; SSoTs im Fleet aus den Referenzdateien | Hypervisor stoppen → `lyndrix.int` antwortet weiter |

M0–M1 sind reine Core-Arbeit und für Marvins Produktivinstanz **sofort** nützlich. M2 und M3
hängen beide nur an M1 und können parallel laufen — das stellt sicher, dass kein Core-Detail
versehentlich Docker- oder k8s-spezifisch wird. M4 ist unabhängig vom Deployment und kann nach M1
Plugin für Plugin erfolgen. M5 ist Infrastruktur und braucht aus dem Core nur `VAULT_NODES`
und den DB-Retry (beides in M1 mitnehmen).

---

## 8. Entscheidungen für Marvin

1. **Eigenes Repo `lyndrix-deploy`** (empfohlen) oder `lyndrix-core/deploy/`?
2. **Sprache des HA-Doku-Baums:** englisch wie die bestehende MkDocs-Site (empfohlen) oder deutsch
   nach Workspace-Konvention?
3. **LB-Optionen in der Doku:** Traefik + HAProxy + keepalived sind Pflicht; **Caddy** (Einsteiger)
   und **pfSense-HAProxy + CARP** (Appliance) aufnehmen? Docker Swarm nur erwähnen oder als Profil?
4. **Treiber `redis`** in M1 mitbauen (kleiner Mehraufwand, gleiche Schnittstelle) oder erst bei Bedarf?
5. **`pinned`-Semantik** für api-Instanzen: 503 „remote“ (einfach, M1) oder transparenter Proxy zum
   Worker (nahtlos, später)?
6. **Cluster-Seite** im Core/Shell (empfohlen: Plattformzustand) oder als Plugin?
7. **Tier 1 als offizieller Support-Stand** (N Core + LB, aber Single-DB/Vault) ausweisen — ja?

---

## 9. Bezug zur Analyse

Alle Befunde mit `file:line`, die Zielarchitektur und die Phasenlogik stehen in
[`high-availability.md`](./high-availability.md); dieses Dokument übersetzt sie in Konfiguration,
Code-Struktur, Artefakte, Doku-Baum und Meilensteine. Die Analyse-Phasen 0–4 entsprechen hier
M0 → M1 → M2/M3 → M4 → M5/M6.
