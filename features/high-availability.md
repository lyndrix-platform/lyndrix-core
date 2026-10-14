# Feature-Plan: Hochverfügbarkeit (HA) für Lyndrix Core + Plugins

**Status:** 🚧 Phase 0 + 1 umgesetzt (Core 1.4.0 / API 1.7.0, siehe Umsetzungsstand in [`high-availability-implementation.md`](./high-availability-implementation.md)); Phasen 2–4 offen
**Erstellt:** 2026-10-14 aus drei parallelen Erhebungen (Core-Zustandsinventar, Plugin-Inventar, Ist-Deployment im `iac-environment`)
**Betroffene Repos:** `lyndrix-core`, `lyndrix-ui`, alle `lyndrix-plugin-*`, SSoT `aac-lyndrix-core` (+ neue SSoTs für DB/Vault/VIP)
**Baut auf:** [`rbac-security-k8s-audit.md`](./rbac-security-k8s-audit.md) Teil C (Multi-Pod-Readiness, Juli 2026) — die dort gelisteten Punkte A–J werden hier geprüft, erweitert (Plugins, Daten-Tier, Secrets-Tier) und in einen Umsetzungspfad gebracht.

> Konvention: Prosa Deutsch, Code/Identifier/APIs Englisch. Alle Befunde sind mit `file:line`
> belegt (Stand Core 1.3.0 / API 1.5.0, Branch `feature/plugin-runtime-status`), damit ohne
> erneutes Research umgesetzt werden kann. Pfade im Core relativ zu `lyndrix-core/app/`,
> in Plugins relativ zum Plugin-Root, im IaC-Workspace mit den Kürzeln
> **AR** = `aac-platform/aac-application-defenitions/applications-repository`,
> **CTL** = `iac-platform/iac-controller/environments`,
> **TE** = `aac-platform/aac-template-engine`.

---

## 0. Kurzfassung

Lyndrix ist heute **ein zustandsbehafteter Prozess** auf **einer VM** mit **einer** MariaDB,
**einem** Vault (File-Storage) und Bind-Mounts auf lokaler Platte — und diese VM wurde laut
Backup-Audit **noch nie erfolgreich gesichert**. Die Verfügbarkeit von Lyndrix ist damit exakt
die Verfügbarkeit von VM 8005 auf `cerberus`.

Der Weg zu „N Backends, N Frontends, N DB-Knoten, N Vault-Knoten“ hat vier Ebenen, die
unabhängig voneinander skaliert werden können, **wenn** der Core drei Dinge bekommt, die er
heute nicht hat:

1. **Eine Koordinationsschicht** (Instanz-Registry, Leader-Lease, TTL-Store, Event-Fan-out) —
   DB-basiert, ohne neue Pflicht-Abhängigkeit; Redis/NATS als optionaler Treiber.
2. **Eine Platzierungs-Semantik für Plugins** (`any | singleton | pinned`), damit Hintergrund-
   arbeit (Probes, Reconciler, Runner) genau einmal läuft statt N-mal.
3. **Externalisierten Zustand**: alles, was heute in Prozess-Speicher oder lokalen Dateien liegt
   (Notifications, OIDC-Zwischenzustände, Rate-Limiter, Plugin-Artefakte, Anhänge), wandert in
   DB, Shared-Secret oder Object-Store.

Daten-Tier und Secrets-Tier sind davon entkoppelt: **MariaDB Galera ×3 hinter MaxScale mit genau
einem Writer** und **Vault/OpenBao mit Raft ×3** funktionieren mit dem heutigen Datenmodell,
sofern alle Schreibzugriffe über einen Knoten laufen (das Modell nutzt `SELECT … FOR UPDATE`,
`GET_LOCK`-Kandidaten und Read-Modify-Write; Multi-Writer-Galera würde das brechen).

**Empfohlene Reihenfolge:** Phase 0 (Backups, Readiness, Boot-Races — macht auch die heutige
Einzelinstanz robust) → Phase 1 (Koordinationsschicht im Core, API 1.6) → Phase 2 (Plugins
anpassen) → Phase 3 (Galera, Raft, Object-Store) → Phase 4 (Deployment-Profil: Compose ×3 auf
den drei Olympus-Nodes **oder** k3s). Details in §10, offene Entscheidungen in §12.

---

## 1. Ist-Zustand: so läuft Lyndrix heute

Quelle: `AR/aac-lyndrix-core/service.yml` (Schema v2), Host-Block `CTL/sites/onprem/stages/prod/hosts.yml:377` (Service-Zuweisung `:504`), Audit-Notizen in `~/gitlab/iac-audit/`.

| Aspekt | Heute |
|---|---|
| Host | `docker-auto` 10.1.130.71 = Proxmox **VM 8005 auf cerberus**, 13 GB RAM, nicht Terraform-verwaltet (`is_managed: false`); teilt sich die VM mit Home Assistant, EMQX, Node-RED, n8n, Zigbee2MQTT |
| Stack | **ein** Compose-Projekt: `aac-lyndrix-core` (ghcr 1.1.1, :8081), `aac-lyndrix-ui` (nginx, :8282, Router `mngm.<domain>`), `aac-lyndrix-db` (MariaDB 12.2.2 aus `TE/catalog/mariadb.yml`, Single), `aac-lyndrix-vault` (`hashicorp/vault:2.0.3`, **File-Storage**, `tls_disable`, Single) |
| Core-Container | kein Healthcheck, `restart: unless-stopped`, mountet `/var/run/docker.sock` (Orchestrator-Runner + Self-Restart), `HOST_*`-Pfade unter `/export/docker/aac-lyndrix-core/…` → **hostgebunden** |
| Volumes | alles Bind-Mounts auf der lokalen VM-Platte: `plugins`→`/app/plugins`, `storage`→`/data/storage`, `secure_data`→`/data/security` (Vault-Keyfile!), `db`→`/var/lib/mysql`, `vault_data`→`/vault/file` |
| Ingress | per-Host-Traefik (`docker_standard`-Profil), Router `lyndrix.int.fam-feser.de` auf Port 8081, IONOS-Cert |
| Backups | **keine** im SSoT (kein `mariadb-dump`, kein Vault-Snapshot); VM 8005 hängt im vzdump-Job „AD - ONPREM“, der sie **nie erfolgreich gesichert hat** (`iac-audit/2026-09-25-backup-abdeckung.md`) |
| Leichen | `docker-auto:/export/docker/aac-lyndrix-core-bak`, `docker-gpu:/export/docker/aac-lyndrix-core`, `docker-dmz:aac-lyndrix-showcase` |
| Doku | `aac-iac-documentation/content/aac-services/aac-lyndrix-core/documentation.md` generiert, veraltet (Image 0.1.3, 2026-06-19) |

Was der Fleet an HA-Bausteinen **hat** und **nicht hat** (Details §11):

| Baustein | Vorhanden? | Wo | HA-fähig wie deployed? |
|---|---|---|---|
| MariaDB | ja, Sidecar je App | `TE/catalog/mariadb.yml` | nein |
| Galera / MaxScale / ProxySQL / Patroni | **nein** | — | — |
| Vault (Lyndrix) | ja, gebündelt | `aac-lyndrix-vault` | nein (File, Single) |
| OpenBao (zentral) | ja, 2.5.2 | `cicd-core` (Hetzner) via Legacy-Rolle; SSoT `aac-hashivault` nur test | nein (File, manuelles Unseal) |
| Redis/Valkey | nur Sidecars | diverse SSoTs | nein |
| NATS / RabbitMQ / Kafka / etcd / Consul | **nein** | — | — |
| MQTT (EMQX 5.8.8) | ja, Single | `docker-auto` | nein |
| MinIO (S3) | ja | `cicd-core` 10.100.1.5 (TF-State) | Single |
| TrueNAS (NFS/CIFS) | ja, onprem | olympus | Single |
| k3s | ja: 3 Master + 2 Worker | 10.100.1.100–121 (Hetzner-Netz) | Control-Plane ja |
| Longhorn / MetalLB (10.100.1.150–190) / cert-manager / Traefik v3 | ja | k3s | ja (innerhalb k3s) |
| GitOps für k8s | Rancher Fleet vorhanden, vom Repo ungenutzt | k3s | — |
| VIP (keepalived / CARP / Anycast) | **nein** | — | — |
| Engine-Replikas / Multi-Host | nur ungenutzte k8s-Templates (`TE/templates/kubernetes/`); Compose kennt kein `deploy:` | — | nein |
| Lyndrix-Metriken | **nein** (Prometheus scrapt nur Traefik/cAdvisor) | `AR/aac-prometheus` | — |

---

## 2. Zielbild: was „HA“ für Lyndrix heißt

**Ziele**

- Kein Single Point of Failure im **App-Tier**: Ausfall einer Core-Instanz, einer UI-Instanz, eines
  DB-Knotens oder eines Vault-Knotens ist für Nutzer unsichtbar (laufende SSE-Streams verbinden
  sich neu, laufende Orchestrator-Jobs dürfen sichtbar „übernommen“ werden).
- **Rolling Restart ohne Downtime** für Core-Releases und Plugin-Updates.
- **Ausfall eines Hypervisors überleben** — konkret motiviert durch hydra (OOM 09-25, kcompactd-Soft-Lockup 10-11, zwei Tage tot).
- **RPO ≤ 1 h, RTO ≤ 15 min** für den Daten-Tier (heute: RPO = ∞, weil kein Backup).
- Jede Ebene unabhängig skalierbar: N Frontends, N Backends, N DB-Knoten, N Vault-Knoten.

**Nicht-Ziele (bewusst)**

- **Kein Cross-Site aktiv/aktiv.** Galera und Raft sind synchron; über den WireGuard-Link
  onprem↔Hetzner wird jeder Commit zur WAN-Roundtrip. Ein HA-Cluster lebt an **einem** Standort;
  der zweite Standort ist Backup-/DR-Ziel.
- **Kein NiceGUI in HA.** Die Legacy-UI ist websocket- und prozessgebunden; HA-Instanzen laufen
  `LYNDRIX_UI_ENGINE=react`. Plugins mit NiceGUI-only-UI (aac-ssot-manager) werden gepinnt oder
  abgelöst.
- **Kein multi-aktiver IaC-Orchestrator.** Er braucht Docker-Socket und Git-Working-Trees; er
  läuft als gepinnte Worker-Instanz (genau eine), API-Instanzen leiten Trigger weiter.

---

## 3. Zielarchitektur (deployment-agnostisch)

```
            DNS lyndrix.int ──► VIP (keepalived)   |   k3s: Traefik LB-IP (MetalLB)
                                        │
                          ┌─────────────┴─────────────┐
                          │ Ingress / TLS / LB        │  Traefik, Health = GET /api/ready
                          └──┬──────────┬──────────┬──┘
             ┌───────────────┘          │          └───────────────┐
      ┌──────┴──────┐           ┌──────┴──────┐            ┌──────┴──────┐
      │ lyndrix-ui  │   × N     │ lyndrix-ui  │            │ lyndrix-ui  │  nginx, stateless
      └──────┬──────┘           └──────┬──────┘            └──────┬──────┘
             └───────────────┬─────────┴──────────┬───────────────┘
                      ┌──────┴──────┐      ┌──────┴──────┐      ┌──────────────────┐
                      │ core api #1 │      │ core api #2 │  …   │ core worker      │ placement:
                      │ (Leader)    │      │             │      │ (Orchestrator,   │ pinned
                      └──────┬──────┘      └──────┬──────┘      │  docker.sock,    │
                             │                    │             │  git working tree│
                             │                    │             └────────┬─────────┘
               ┌─────────────┴────────────────────┴──────────────────────┴───────────┐
               │ Koordination (in der DB, Redis/NATS optional):                      │
               │ cluster_instances · cluster_leases · cluster_kv · cluster_events    │
               │ → Leader-Lease, Plugin-Leases, TTL-Store, Bus-Bridge + SSE-Relay    │
               └─────────────┬────────────────────┬──────────────────────┬───────────┘
                      ┌──────┴──────┐      ┌──────┴──────┐        ┌──────┴──────┐
                      │ MaxScale    │      │ Vault/Bao   │        │ Object Store│
                      │ (1 Writer)  │      │ Raft × 3    │        │ S3 / NFS    │
                      └──────┬──────┘      └─────────────┘        └─────────────┘
                      ┌──────┴──────┐
                      │ Galera × 3  │
                      └─────────────┘
```

**Vertrag je Komponente**

| Komponente | Skalierung | Zustand | Harte Anforderung |
|---|---|---|---|
| `lyndrix-ui` | N, beliebig | keiner | `LYNDRIX_BACKEND_URL` → Backend-Service/VIP; gleiche Origin |
| `core api` | N, beliebig | nur abgeleitet (Registry, Router, Caches) | React-only; `/api/ready`; Koordination über DB; identische Plugin-Artefakte; gleiches `STORAGE_SECRET` |
| `core` Leader | genau 1 (Lease, Failover automatisch) | Lease-Row | führt Seeds/Migration, Reconciler, Update-Checker, Messaging-Outbox, `singleton`-Plugin-Jobs aus |
| `core worker` | genau 1 je Rolle (gepinnt) | Git-Tree, Docker-Socket, Job-Logs | Orchestrator; empfängt Trigger über `cluster_events`; `owner_instance` auf Jobs |
| MariaDB | 3 Knoten Galera, **1 Writer** via MaxScale | alles Persistente | jede Tabelle hat PK (Galera); DDL nur im Migrationsjob; Retry auf 1213/1205/2006/2013 |
| Vault/OpenBao | 3 Knoten Raft | Secrets, Settings | Unseal aller Knoten durch Lyndrix-Leader (oder Transit); Keyfile als Shared-Secret; Nicht-Root-Token |
| Object Store | 1 (S3/MinIO oder NFS), degradiert gnädig | Anhänge, Hintergründe, Themes, Plugin-Zips | `ctx.storage`-Abstraktion mit Local/S3-Treiber |
| Koordination | in der DB (Phase 1), Redis-Treiber optional | Leases, TTL-KV, Event-Log | Poll ≤ 1 s; Leases 30 s TTL / 10 s Heartbeat |

---

## 4. Core-Befunde: was mit 2+ Instanzen bricht

### 4.1 Drei Blocker, die man leicht übersieht

1. **Gleichzeitiger Erst-Boot zweier Instanzen lässt eine für immer hängen.** `_seed_default_groups`
   (`core/components/auth/logic/auth_service.py:99-119`) hat kein Error-Handling, `Group.name` ist
   unique (`auth/logic/models.py:89`). Der Verlierer wirft in `_bootstrap_iam`; `initialize_iam`
   loggt nur (`:86-87`) und emittiert nie `iam:ready` → Instanz bleibt in `waiting_core`, kein Retry.
   Gleiches Fenster für `create_all` (`:91`), `_ensure_plugin_state_schema` (`plugins/logic/manager.py:979-1008`,
   `ALTER` ungeschützt → `_activate_saved_plugins` bricht ab, **keine Plugins starten**) und
   `PluginState`-Insert (`manager.py:564-647`, IntegrityError ungefangen).
2. **`/api/health` meldet `ok` während des Boots.** Middleware lässt `/api` beim Booten durch
   (`main.py:230-253`); bei leerer Registry bleibt `worst == "ok"` (`main.py:418-463`). Es gibt keine
   Readiness/Liveness-Trennung, DB/Vault/Boot-Phase werden nicht geprüft → ein Load Balancer
   schickt Traffic auf eine Instanz, die noch nicht bereit ist (oder deren Vault gesealt ist).
3. **React-SSO funktioniert nur mit aktiviertem NiceGUI.** `/auth/callback/oidc` ist nur unter
   `_nicegui_enabled` registriert (`main.py:341-343`, `auth/ui/routes.py:104`). HA-Instanzen laufen
   React-only → SSO wäre tot. Außerdem liegt der gesamte OIDC-Zwischenzustand im Prozess-Speicher
   (`auth/logic/providers/oidc.py:70-76`, `auth/ui/routes.py:17-18`, `core/api/auth_api.py:199-215`),
   und der Flow spannt drei Requests, die dieselbe Instanz treffen müssen.

### 4.2 Alle Befunde, gerankt

Spalte „Juli“ = Punkt aus `rbac-security-k8s-audit.md` Teil C. ✅ = seit Juli erledigt.

| # | Bereich | Befund (`file:line`) | Bricht bei N Instanzen | Juli | Kleinster Fix |
|---|---|---|---|---|---|
| 1 | Event-Bus | `GlobalEventBus` rein in-memory, `emit()` nur lokal (`core/bus.py:7-12, 97-129`); `ctx.request()` matcht Replies über prozesslokale Futures (`plugins/logic/context.py:80, 217-268`) | `plugin:files_changed`, `plugin:state_changed`, `plugin:runtime_status`, `ui:needs_refresh`, Theme-/Settings-Änderungen, `system:maintenance_mode` erreichen nur die emittierende Instanz | A | lokaler Bus bleibt; **Bridge** für eine Allowlist von Broadcast-Topics über `cluster_events` mit `origin`-Instanz-ID; `ctx.request` bleibt lokal |
| 2 | SSE | `_SSE_QUEUES` prozesslokal (`core/api/events_api.py:38, 153-175`); `system:metrics_update` zeigt Werte der haltenden Instanz | Client an B bekommt nichts von A (Installs, `notification:new`, Runtime-Status) | B | `_publish` aus der Bridge speisen (jede Instanz relayt alle Allowlist-Topics in ihre Streams) |
| ✅ | SSE-Ticket | HMAC aus `STORAGE_SECRET` (`events_api.py:66-87`), deterministisch, Random nur bei leerem Secret | — | C | erledigt; nur gleiches `STORAGE_SECRET` überall |
| 3 | Boot-Races | §4.1 Nr. 1; zusätzlich `_seed_or_update_user` Read-Modify-Write (`auth_service.py:246-281`), Rollen-Ledger JSON-RMW (`role_registry.py:159-260`) | eine Instanz hängt / Plugins starten nicht | I | Bootstrap + DDL + Seeds unter `GET_LOCK('lyndrix_bootstrap', 60)` **oder** einmaliger Migrationsjob (Alembic); Seeds `INSERT … ON DUPLICATE KEY` / IntegrityError-tolerant |
| 4 | DDL überall | `create_all` + `ALTER` auf **jeder** Instanz bei **jedem** Boot und Reconnect: `auth_service.py:91, 144-172`, `manager.py:979-1008`, `notification_router/logic/router_service.py:78-110`, `custom_repo_service.py:46`, `preferences_service.py:60`, `plugin_service.py:215`, messaging `init_db` | Races; auf Galera ist jedes DDL ein TOI-Statement, das den Cluster anhält | I | Migrationsjob; Instanzen nur `checkfirst` |
| 5 | DB-Reconnect | Watchdog `SELECT 1`/15 s (`database/logic/db_service.py:137-150`); Loop gibt nach 30×5 s **endgültig** auf → Maintenance-Mode (`:94-130`); Erfolg **re-emittiert `db:connected`** (`:106`) → IAM-Bootstrap + `load_all()` laufen erneut (`auth_service.py:59`, `boot_service.py:42, 52`) | MaxScale-Failover (10–30 s) ist ok, aber ein längerer Ausfall killt die Instanz dauerhaft; Re-Bootstrap im Betrieb | — | Retry endlos mit Backoff; Reconnect emittiert `db:reconnected` ohne Re-Bootstrap |
| 6 | DB-Verbindung | ein `DB_HOST` (`config.py:47, 186-187`), Default-Pool 5+10, kein `pool_recycle` (`db_service.py:79-83`); `last_used_at` bei **jedem** Request geschrieben (`api_key_service.py:142-150`, 1020-Konflikte schon gesehen) | Hot-Row → Zertifizierungskonflikte auf Galera | — | `DB_HOST` → MaxScale; `pool_recycle`; `last_used_at` nur wenn > 60 s alt |
| 7 | Vault-Unseal | Statemachine läuft einmal auf `system:started`, Retry nur bei Exception (`vault/logic/vault_service.py:66-114`); Unseal über `/api/vault/unseal` auf A lässt B in `needs_unseal`; Auto-Unseal braucht `LYNDRIX_MASTER_KEY` **und** lokales Keyfile `/data/security/vault_keys.enc` (`auto_unseal.py:24-34`, `vault_init.py:65-84`); `is_connected` wird **nie** wieder false, `read_secret_dict` liefert `{}` bei Fehler (`kv_helper.py:157-179`) | nur ein Vault-Knoten wird entsiegelt; Instanzen ohne Keyfile hängen; gesealter Vault → Settings lesen still als leer | F | Leader pollt `sys/health` **aller** `VAULT_NODES` und entsiegelt jeden; Keyfile als Shared-Secret; `vault:status_changed` emittieren (ist allowlisted, wird nie gesendet); Nicht-Root-Token (AppRole) |
| 8 | Settings-Drift | `hydrate_from_vault()` einmal auf `vault:opened` (`config.py:351-391`); `POST /api/system/config` → Vault-RMW ohne CAS + `setattr` **lokal** (`settings/logic/system_config_service.py:77-91`); `ctx.set_secret` nur prozesslokal gelockt (`context.py:498-529`) | andere Instanzen behalten alte `DEFAULT_THEME_ID`, `LOG_LEVEL`, Auth-Settings bis Restart | — | `settings:changed` über Bridge → Peers `hydrate_from_vault`; KV-v2 `cas` |
| 9 | Plugin-Artefakte | Code in `/app/plugins` (`plugin_service.py:57`, `manager.py:31, 89`); Install = Zip → Tempdir → Swap + `pip --target vendor` (`plugin_service.py:686-905`); `plugin:files_changed` lädt nur lokal (`manager.py:271-339`); `toggle_module` nur lokal (`manager.py:651-719`) | A installiert X, B hat keine Dateien → 404 auf Router und Bundle; `_activate_saved_plugins` aktiviert nur, was auf Platte liegt; Reconcile kennt nur `LYNDRIX_PLUGINS_DESIRED`, nicht Marketplace-Installs | F, G | Plugin-Code = **immutable Artefakt**: ins Image backen **oder** jede Instanz reconciled `/app/plugins` aus `plugin_states` (desired version) auf `plugin:desired_changed` + periodisch |
| 10 | Core-Hintergrundjobs | Collection-Watcher 900 s (`plugin_service.py:295-328`), Update-Checker (`:342-420`, Dedupe `_latest_known` prozesslokal → N Benachrichtigungen), Messaging-Expiry 300/600 s (`messaging/entrypoint.py:79-90`), Gateway-Retry-Queues in-memory (`messaging/logic/gateway.py:54-120`) | N-fach; Retry-Queue geht bei Restart verloren | G | Leader-Lease; Retry → DB-Outbox |
| 11 | Notifications | `deque(maxlen=500)` + `/data/storage/notifications.json`, einmal beim Import geladen (`notifications/logic/notification_service.py:37-104`) | jede Instanz eigene Historie/Lesestatus; Shared-File = Last-Writer-Wins | — | DB-Tabellen + `notification:new` über Bridge |
| 12 | Messaging-Korrelation | `messaging_pending_actions` in DB, aber `resolve()/get()` lesen **nur** den Cache (`messaging/logic/correlation.py:141-172`); `StreamBridge` in-memory | Discord-Button-Callback landet auf B → „correlation_id not found“, Aktion verloren | — | DB-Fallback bei Cache-Miss, atomar `UPDATE … WHERE resolved_at IS NULL` |
| 13 | Rate-Limiter | Login 5/60 s in-memory (`auth_api.py:25`, `vault/logic/rate_limit.py:97-134`), Key `username|client.host`; uvicorn **ohne** `--forwarded-allow-ips` (`docker/Dockerfile:67`) → hinter Traefik ist `client.host` die LB-IP | Limit ×N; **heute schon**: ein Bucket pro Username für alle Clients → triviale Aussperrung | E | `--forwarded-allow-ips` + `X-Forwarded-For`; Zähler in `cluster_kv` |
| 14 | OIDC/SSO | §4.1 Nr. 3 | Callback auf A, Token-Exchange auf B → „invalid code“ | D | Callback aus dem NiceGUI-Gate; State/PKCE/Codes in `cluster_kv` mit TTL |
| 15 | Self-Restart | `POST /api/system/restart` → `docker restart <own>` über lokalen Socket (`main.py:556-595`, `sockets/providers/docker_provider.py:17-33, 82-91`) | startet die zufällig antwortende Instanz; braucht Socket | H | HA-Modus: `cluster_events` „restart requested“ → jede Instanz beendet sich geordnet (exit 0, Restart-Policy) **oder** Orchestrator-Rollout |
| 16 | Lokale Platte | `/data/security/vault_keys.enc`; `/app/plugins`; `notifications.json`; `/data/storage/user_backgrounds` (`auth_api.py:49-50, 629`); Theme-Uploads in `app/assets/themes/` **im Image-Layer** (`themes_api.py:18, 142-193`); `/app/logs/lyndrix.log` RotatingFileHandler (`core/logger.py:122-135`); `/app/.nicegui/storage-user-*.json`; `/data/storage/git_repos` (`git/logic/git_service.py:46`) | pro Instanz divergent; Theme-Upload geht bei Redeploy verloren; N Writer auf einer Log-Datei korrumpieren | F | Keyfile = Secret; Backgrounds/Themes/Anhänge → Object-Store; Logs → stdout; Git nur auf Worker |
| 17 | NiceGUI | `ui.run_with(storage_secret=…)` (`main.py:645`), Storage lokal, Websocket je Client; NiceGUI-only: `/`, `/login`, `/auth/complete`, `/dashboard`, `/settings`, `/plugins`, `/setup`, `/unseal` + Plugin-Pages | ohne Sticky-Session bricht jede Seite; Login auf A unbekannt auf B | J | HA = `LYNDRIX_UI_ENGINE=react`; REST-Äquivalente existieren für alles außer dem OIDC-Callback (#14) |
| 18 | Health | ein Endpoint, ruft `health()` **jedes** aktiven Plugins bei **jedem** Request (`main.py:399-470`, 5 s Timeout je Plugin) | LB-Probe × Dashboards × N = Last; keine Boot-/DB-/Vault-Prüfung | — | `/api/live`, `/api/ready` (Boot-Phase ∧ DB ∧ Vault, ohne Plugins), `/api/health` tief + 10 s gecacht |
| 19 | Caches/Invalidierung | Permission-Cache 5 s (`access_service.py:33-34`), System-Key 30 s (`core/api/security.py:56-57`), Provider-Registry Reinit lokal (`auth_service.py:208-217`), i18n-Version aus `mtime` (`core/i18n.py:130-153`) → 200/304-Flackern | begrenzte Staleness; Cache-Churn | — | Invalidierungs-Events über Bridge; i18n-Hash über Inhalt statt mtime |
| 20 | Runtime-Status (API 1.5) | per Instanz (`context.py:83`, `router_registry.py:58-88`) | Maintenance auf A → kein 503 auf B; Warm-up-Panel flackert je antwortender Instanz | — | 503-Gate bleibt lokal (richtig); Status mit `instance_id` broadcasten, Shell aggregiert |

**Was bereits HA-tauglich ist:** REST-Sessions sind DB-Tokens (`UserApiKey`, SHA-256, kein JWT,
kein Ablauf) → React-UI + REST sind instanz-agnostisch; SSE-Tickets (✅ C); `ctx.get_setting`
liest Vault bei jedem Aufruf; `max_statement_time` pro Connection; `plugin_states` in der DB.

---

## 5. Plugin-Befunde

Allgemein gilt für alle: der Bus ist prozesslokal, `ctx.request()` funktioniert nur, weil
Requester und Responder im selben Prozess sitzen (das bleibt so — Responder-Plugins laufen auf
jeder Instanz). **Sobald der Bus gebridged wird, läuft jeder seiteneffektbehaftete Subscriber
N-mal** → die Bridge darf nur eine Allowlist transportieren, Plugin-Topics bleiben lokal, außer
das Plugin emittiert explizit `cluster=True`.

| Plugin | Rating | Kernbefund (`file:line`) | Kleinster Fix |
|---|---|---|---|
| **monitoring** | 🔴 BLOCKER | Scheduler je Monitor pro Instanz (`app/controller/service.py:47`), Semaphore/Inflight/Circuit-Breaker prozesslokal (`:54, 58, 81-83`); jede Probe N-mal; `_bump_daily_aggregate` RMW ohne Lock (`:509-536`); **kein UNIQUE(monitor_id, day)** (`app/model/models.py:73-80`) → Duplikat-Rows; `monitoring:state_changed` N-mal (`:835-848`); Inventory-Ingest resynct nur den lokalen Scheduler (`:469-486`); 503-Gate je Instanz | Prober hinter `ctx.lease("monitoring:prober")` (später Sharding `hash(monitor_id) % N`); `UNIQUE(monitor_id, day)` + atomares `UPDATE … SET up_samples = up_samples + 1`; `monitoring:monitors_changed` als Cluster-Event |
| **iac-orchestrator** | 🔴 BLOCKER (pinnen) | In-Memory `state`/Dispatch-Locks/Semaphore(1) (`app/controller/service.py:25-31`, `engine.py:905-922`); Dispatch-Guard = Set + `RUNNING`-Check, dann `create_job` → TOCTOU (`engine.py:1355-1445`); **Startup-Reconciliation** resumt oder FAILt RUNNING-Jobs **anderer** Instanzen (`service.py:103-136`, `engine.py:1873`); Docker-CLI `logs -f/wait/rm/run/login` lokal, feste Containernamen `aac-runner-*`/`iac-tf-*` (`engine.py:1946-2032, 2875-2925`); Git-Trees `/data/storage/git_repos/*` mit In-Process-Lock; Job-Logs lokal; SSE `stream/jobs` aus `active_tasks`; Webhooks `POST /api/iac/webhook/*` öffentlich (`entrypoint.py:237`), Hook-URL aus Vault-Setting | `placement: pinned:runner`; `owner_instance` auf `iac_orchestrator_jobs`, Reconciliation nur eigene Jobs; Trigger von API-Instanzen via Cluster-Event; Dispatch-Reservierung als DB-Row; langfristig Runner als k8s-Jobs |
| **aac-ssot-manager** | 🔴 BLOCKER | NiceGUI-only, prozessglobaler Editierpuffer `state` (`entrypoint.py:42-55`), schreibt direkt `/data/storage/git_repos/iac_env_main` (`:57, 72-101`); registriert `/iac` — **Kollision** mit dem Orchestrator | ablösen (React-Editor im Orchestrator) oder mit dem Worker pinnen; Puffer je Session |
| **meeting-bingo** | 🔴 BLOCKER (Feature) | Spiele/Boards nur im Prozess (`app/logic/service.py:57-62`); Scoreboard als Vault-Blob LWW (`:91-130`) | Sessions in DB-Tabelle |
| **docker-manager** | 🟡 NEEDS-WORK | Hostliste = Vault-Blob, einmal geladen, jede Mutation schreibt die ganze Liste (`app/logic/service.py:51-52, 123-128`), IDs `max+1` lokal (`:199`) | DB-Tabelle mit UNIQUE(name), oder Vault bei jedem Read + CAS |
| **server-manager** | 🟡 NEEDS-WORK | `ALTER … ADD COLUMN` **ohne** `IF NOT EXISTS` (`app/model/database.py:25-70`) → Verlierer bleibt `_ready=False` bis zum nächsten `db:connected`; Catalog-Pfad nicht persistiert (`service.py:100-104`) | `IF NOT EXISTS`; Pfad in Settings |
| **entity-registry** | 🟡 NEEDS-WORK | `_create` verliert Race → `RegistryError` ohne Retry (`app/logic/service.py:406-429`) → Hosts ohne `ci_id` bis zum nächsten Publish; `registry_conflicts.fingerprint` nicht unique (`models.py:128`, `:462-495` Check-then-Insert) | Retry durch Re-Match; `UNIQUE(fingerprint)` + IntegrityError fangen |
| **itsm** | 🟡 NEEDS-WORK | Anhänge auf lokaler Platte `/data/storage/itsm/attachments` (`service.py:86`); Nummernvergabe `SELECT … FOR UPDATE` + Retry (`:121-131, 285-294`) **cluster-sicher bei Single-Writer**; `version`-Checks vorhanden | `attachment_dir` → Object-Store |
| **discord-notifier** | 🟡 NEEDS-WORK | `_message_store` notification_id→message_id in-memory (`app/logic/adapter.py:137`) → Live-Edits werden neue Posts; 429-Handling pro Instanz (`:287, 391`); Interactions-Endpoint braucht stabile URL | Store in DB |
| **external-services** | 🟡 NEEDS-WORK | Sidebar in Core-Registry nur lokal (`routing.py:66-93`); **`health()` ruft `ensure_table()` = DDL bei jedem Poll** (`service.py:52-82`) | Sidebar aus DB je Request; DDL aus `health()` raus |
| collection / plugin-ui-template | ⚪ | kein Runtime-Code | — |

**Hintergrundjobs über alle Plugins** (N-fach ohne Lease):

| Plugin | Job | Intervall | Duplikat-sicher? | Guard |
|---|---|---|---|---|
| monitoring | Probe je Monitor | `interval_seconds` | **nein** (N Probes, verlorene Updates, N `state_changed`) | Lease oder Sharding + atomare Zähler |
| monitoring | Startup-Sweep | je Boot/Ingest | nein | Lease |
| monitoring | Prune / Rollups / Uptime-Windows | stündlich, täglich | ja, aber N× Last | Lease (optional) |
| iac-orchestrator | Startup-Reconciliation / `resume_bulk_rollout` | je Boot | **nein** (doppelte Rollouts oder fremde Jobs FAILed) | `owner_instance` / pinned |
| iac-orchestrator | Pipeline je `iac:webhook_verified` | je Event | **nein** (TOCTOU, Semaphore lokal) | DB-Reservierung / pinned |
| iac-orchestrator | Webhook-Sync-Loop | 1800 s | ja (N× GitLab-API) | Lease |
| iac-orchestrator | Inventory-Publish (`emit_iac_inventory_updated`, Monitoring-Sync) | je Boot | meist (Registry-Insert-Race, Vault-RMW im docker-manager) | Leader-only |
| docker-manager | `sync_orchestrator_hosts` | je Publish | idempotent, Vault-RMW racet | DB-Tabelle |
| übrige | keine | — | — | — |

**Hostgebundene Ressourcen:** Orchestrator (Docker-Daemon via Socket-Manager + CLI; Git-Trees;
`.iac_keys`, `secrets.json`; Job-Logs; `terraform-providers`) → **pinnen**; aac-ssot-manager
(gleicher Git-Tree) → mit dem Worker pinnen; ITSM-Anhänge → Shared/Object-Store **Pflicht**;
Kataloge (server-manager/itsm/registry YAML) → identische Artefakte; monitoring braucht `NET_RAW`
je Instanz, Probe-Quell-IP variiert (Firewall-ACLs!); docker-manager/discord nur Outbound.

**Stabile öffentliche Adressen nötig:** `/api/iac/webhook/{gitlab,sync,gitlab/test-host/*}`,
Discord `/api/plugins/lyndrix.plugin.discord/interactions`, monitoring `/passive` + `/inventory-sync`,
server-manager `/servers/external/{source}/{id}` → alles über die VIP; das Vault-Setting
`iac_lyndrix_base_url` zeigt auf die VIP.

---

## 6. Daten-Tier: N Datenbanken

**Entscheidung: Galera ×3 mit genau einem Writer (MaxScale `readwritesplit` mit
`master_accept_reads=true`, zunächst alle Reads auf den Writer).** Begründung:

- Das Lyndrix-Datenmodell setzt Single-Writer voraus: `SELECT … FOR UPDATE` (ITSM-Counter),
  `GET_LOCK` (geplant für Bootstrap), Read-Modify-Write auf JSON-Spalten (Gruppen-Permissions,
  Rollen-Ledger). Auf Galera sind Row-Locks **nicht clusterweit** (Konflikt erst beim Commit →
  Deadlock 1213), `GET_LOCK` wirkt nur lokal. Alle Sessions auf einem Knoten = Semantik wie heute.
- Galera gibt synchrone Replikation ohne Lag + automatisches Failover (MaxScale wählt neuen
  Writer in Sekunden) — Primary/Replica mit asynchroner Replikation hätte beim Failover Datenverlust.
- Alternative Primary + Semi-Sync-Replica + MaxScale `mariadbmon` (auto_failover) ist einfacher,
  aber ein Replica-Rejoin ist Handarbeit; bei drei Knoten lohnt Galera.

**Anforderungen an die App (Core-Arbeitspaket):**

| Anforderung | Warum | Wo |
|---|---|---|
| Jede Tabelle hat einen PK | Galera-Pflicht (sonst Replikationsfehler) | heute erfüllt (prüfen bei neuen Plugin-Tabellen, Lint in `check_plugin_*`) |
| DDL nur im Migrationsjob, nie in `setup()`/`health()` | TOI-DDL hält den Cluster an; external-services macht DDL bei jedem Health-Poll | §4 #4, §5 |
| Retry-Wrapper für 1213 (Deadlock), 1205 (Lock wait), 2006/2013 (Connection lost beim Failover) | Failover sichtbar als abgebrochene Transaktion | `db_service` `run_with_retry(fn, attempts=3)`; ORM-Sessions kurz halten |
| `last_used_at` throttlen | Hot-Row-Writes = Zertifizierungskonflikte | `api_key_service.py:142-150` |
| `pool_recycle=300`, `pool_pre_ping` (vorhanden) | MaxScale/Galera trennen idle Verbindungen | `db_service.py:79-83` |
| `DB_HOST` = MaxScale-Endpunkt (oder k8s-Service), `DB_HOSTS` optional für Fallback-Liste | ein Endpunkt, Failover im Proxy | `config.py` |
| Batch-Jobs in Chunks (vorhanden: Prune 20 k) | große Transaktionen = große Writesets (`wsrep_max_ws_size`) | monitoring |
| `wsrep_sync_wait=1` **nur** wenn Reads später auf andere Knoten gehen | Read-your-writes | MaxScale-Konfig |

**Deployment-Optionen:** Compose: `TE/catalog/mariadb.yml` um Galera-Variante erweitern
(`mariadb:12` mit `wsrep_*`, Ports 4567/4568/4444, `galera_new_cluster` nur beim Bootstrap) als
neuer SSoT `aac-lyndrix-db` **je Host**, plus `aac-maxscale` (ein MaxScale je Host, lokal
angesprochen → kein zusätzlicher SPOF). k3s: `mariadb-operator` (CRD `MariaDB` mit `galera:` +
`MaxScale`-CRD) auf Longhorn — fertige Lösung inkl. Backups (`Backup`-CRD) und Rolling-Updates.
**Migration:** `mariadb-dump --single-transaction` von `aac-lyndrix-db` → Import in Galera-Node 1
→ Nodes 2/3 joinen per SST.

**Backups (Phase 0, unabhängig von HA):** nächtlicher `mariadb-dump` (oder `mariabackup`) in den
Object-Store/TrueNAS + Vault-Snapshot (`vault operator raft snapshot save` bzw. File-Storage-Tar
mit angehaltenem Vault) + Restore-Übung dokumentiert. vzdump bleibt die zweite Ebene.

---

## 7. Secrets-Tier: N Vaults

**Entscheidung: Raft Integrated Storage ×3, Unseal-Verantwortung bleibt bei Lyndrix.**

- Vault-Raft braucht jeden Knoten entsiegelt; Standbys forwarden API-Requests an den Active-Node.
  Ein einfacher LB über alle drei Knoten reicht, wenn alle entsiegelt sind (Health-Check:
  `sys/health` liefert 200 active / 429 standby / 503 sealed → LB-Policy „200 oder 429 = gesund“).
- **Core-Änderung:** `VAULT_NODES` (Liste) neben `VAULT_URL`; der **Leader** pollt `sys/health`
  jedes Knotens (10 s), entsiegelt gesealte Knoten mit dem Key aus dem Keyfile, emittiert
  `vault:status_changed` (Topic ist allowlisted, wird heute nie gesendet) und setzt
  `is_connected`/`ui_state`/Maintenance-Mode korrekt (§4 #7). Die Init-Statemachine akzeptiert
  `VaultAlreadyInitializedError` als „Peer war schneller“.
- **Keyfile:** `/data/security/vault_keys.enc` ist Argon2+AES-GCM mit `LYNDRIX_MASTER_KEY`
  verschlüsselt → kann als k8s-Secret / Shared-Secret neben dem Master-Key liegen, ohne das
  Sicherheitsniveau zu senken (beides zusammen = heute auch beides auf einer VM). Alternative
  **Transit-Auto-Unseal** gegen das zentrale OpenBao auf `cicd-core` — macht onprem-Lyndrix vom
  WAN abhängig → nur wenn OpenBao selbst HA wird.
- **Token:** heute Root-Token aus dem Keyfile (`vault_service.py:47-64`). HA-Paket: AppRole mit
  `role_id`/`secret_id` aus Env/Secret, periodischer Token, Renewal-Loop. Kein Pflichtteil für HA,
  aber derselbe Code-Pfad — mitnehmen.
- **Vault vs. OpenBao:** `hashicorp/vault:2.0.3` ist BSL-lizenziert; OpenBao (im Fleet als
  „aac-hashivault“ 2.5.2) ist API-kompatibel zu `hvac` und hat Raft. Empfehlung: OpenBao-Raft im
  Lyndrix-Stack **evaluieren** (`vault operator migrate` file→raft funktioniert in beiden).
- **Migration:** `operator migrate -config` (file → raft) auf Knoten 1, dann Knoten 2/3 joinen;
  Keyfile bleibt gültig (gleicher Unseal-Key).

---

## 8. Frontend-Tier: N Frontends

`lyndrix-ui` ist nginx + statische Dateien + Reverse-Proxy auf `LYNDRIX_BACKEND_URL` (gleiche
Origin, kein CORS). **Heute schon beliebig skalierbar.** Anforderungen:

- `LYNDRIX_BACKEND_URL` → Backend-VIP/Service, nicht Container-Name einer Instanz.
- Ingress-Health auf `/` (nginx) reicht; Readiness des Backends prüft der Ingress separat.
- SSE durch nginx: `proxy_buffering off`, `proxy_read_timeout 3600s` — **vorhanden** in
  `lyndrix-ui/docker/lyndrix-proxy.conf:19-21`.
- Die Android-App (TWA) zeigt auf einen Hostnamen → VIP.
- Plugin-React-Bundles kommen vom Backend (`/api/plugins/<id>/static/`) → identische
  Plugin-Artefakte auf allen Backend-Instanzen (§4 #9), sonst 404-Flackern.

---

## 9. Backend-Tier: N Backends — die Koordinationsschicht (Core API 1.6)

### 9.1 Warum DB-basiert zuerst, nicht Redis

- Marvins Grundregel „kein Plugin darf ein anderes *brauchen*“ gilt analog für den Core: HA darf
  keine **neue Pflicht-Abhängigkeit** einführen, die selbst erst HA gemacht werden muss (Redis
  Sentinel/Cluster = ein weiterer Cluster, der im Fleet nicht existiert).
- Die DB ist in HA ohnehin da und hochverfügbar. Lyndrix' Event-Rate ist klein (Runtime-Status
  koalesziert 1/s, Notifications, State-Changes) → Polling mit 500 ms–1 s ist unproblematisch.
- Die Schicht ist eine **Abstraktion** (`ClusterCoordinator`) mit Treiber `db` (Phase 1) und
  optional `redis` (später, für Sub-100-ms-Fan-out und NiceGUI-Storage).

### 9.2 Tabellen (Treiber `db`)

```sql
cluster_instances(instance_id PK, hostname, role, core_version, started_at, heartbeat_at)
cluster_leases   (name PK, holder, acquired_at, expires_at)
   -- Erwerb/Verlängerung atomar: UPDATE … SET holder=:me, expires_at=NOW()+30s
   --   WHERE name=:n AND (holder=:me OR expires_at < NOW()); Heartbeat alle 10 s
cluster_kv       (k PK, v JSON, expires_at)           -- OIDC-State, SSO-Codes, Limiter-Zähler
cluster_events   (id BIGINT AUTO_INCREMENT PK, topic, payload JSON, origin, created_at)
   -- jede Instanz pollt WHERE id > :last_seen (≤ 1 s), ignoriert origin == self; Prune > 1 h
```

Leader = Halter von `cluster_leases['core:leader']`. Verlust der Lease (Instanz tot) → nach ≤ 30 s
übernimmt eine andere; alle Leader-Aufgaben müssen **idempotent und wiederaufnehmbar** sein.

### 9.3 Plugin-API-Erweiterung

```python
manifest = ModuleManifest(
    ...,
    placement="any",            # any | singleton | pinned:<role>   (default: any)
)

def setup(ctx):
    ctx.register_routes(router)                       # läuft auf jeder Instanz (reads/writes via DB)
    ctx.on_lease("monitoring:prober",                 # genau eine Instanz hält die Lease
                 acquired=service.start_probing, lost=service.stop_probing)
    ctx.emit("monitoring:monitors_changed", {...}, cluster=True)   # erreicht alle Instanzen
    if ctx.is_leader: ...                             # Core-Leader (für Einmal-Aufgaben)

async with ctx.lease("itsm:connector_poll") as held:  # Einmal-Arbeit, held=False → überspringen
    ...
ctx.instance_id, ctx.cluster_size                     # Diagnose, Metrik-Labels
```

- `placement: singleton` → der Core ruft `setup()` auf jeder Instanz (Routen!), aber die Lease
  `plugin:<id>:singleton` wird automatisch gehalten; `ctx.on_lease`-Callbacks folgen ihr.
- `placement: pinned:<role>` → `setup()` läuft nur auf Instanzen mit `LYNDRIX_INSTANCE_ROLES`
  ⊇ `{role}`; API-Instanzen antworten für dieses Plugin mit 503 `X-Plugin-Runtime: {"phase":
  "remote"}` **oder** proxien (Entscheidung §12). Trigger an den Worker gehen über
  `ctx.emit(..., cluster=True)`.
- Alte Cores: Plugins guarden mit `getattr(ctx, "on_lease", None)` wie bei `set_status`.

### 9.4 Core-Umbauten (Arbeitspaket Phase 1)

| Paket | Inhalt | Dateien |
|---|---|---|
| Coordinator | Tabellen §9.2, Instanz-Heartbeat (10 s), Leader-Lease, `lease()`/`on_lease()`, `kv_set/get` mit TTL, `publish()`/Poll-Loop | neu `core/components/cluster/logic/coordinator.py`, `core/api/__init__.py` Export |
| Bus-Bridge | `event_bus.emit(topic, payload, cluster=True)`; Allowlist Core-Topics: `plugin:state_changed`, `plugin:runtime_status`, `plugin:desired_changed`, `ui:needs_refresh`, `notification:new`, `settings:changed`, `auth:providers_changed`, `theme:changed`, `system:maintenance_mode`, `system:restart_requested`; eingehende Events werden lokal re-emittiert mit `payload["_origin"]` | `core/bus.py`, Coordinator |
| SSE-Relay | `_publish` auch aus Bridge-Events; `system:metrics_update` mit `instance_id` | `core/api/events_api.py:153-175` |
| Readiness | `/api/live`, `/api/ready` (Boot-Phase ∧ `db.is_connected` ∧ `vault.is_connected`, keine Plugins), `/api/health` 10 s Cache; Boot-Phase exponieren | `main.py:399-470`, `boot_service.py` |
| Bootstrap | `GET_LOCK('lyndrix_bootstrap')` um `create_all`/Seeds/`ALTER`; Seeds idempotent; **oder** `scripts/migrate.py` als Init-Job (Alembic) und Instanzen nur `checkfirst` | `auth_service.py:86-172`, `manager.py:564-647, 979-1008`, `router_service.py:78-110` |
| DB-Resilienz | Reconnect endlos mit Backoff; `db:reconnected` statt Re-Bootstrap; `pool_recycle`; `run_with_retry`; `last_used_at`-Throttle | `db_service.py`, `boot_service.py:42-52`, `api_key_service.py:142-150` |
| Vault-HA | `VAULT_NODES`, Health-Poll + Multi-Node-Unseal auf dem Leader, `vault:status_changed`, Keyfile aus Secret-Pfad, AppRole optional | `vault/logic/*`, `config.py:195-196` |
| Settings-Propagation | `settings:changed` → `hydrate_from_vault()` auf Peers; KV-v2 `cas` in `system_config_service` und `ctx.set_secret` | `settings/logic/system_config_service.py:77-91`, `context.py:498-529` |
| Auth-Zustand | OIDC-State/PKCE/Handoffs/SSO-Codes → `cluster_kv`; Login-/Unseal-Limiter → `cluster_kv`; `--forwarded-allow-ips`; OIDC-Callback aus dem NiceGUI-Gate | `auth/logic/providers/oidc.py:70-76`, `auth_api.py:25, 199-215`, `main.py:341-343`, `docker/Dockerfile:67` |
| Notifications/Messaging | Notifications + Lesestatus in DB-Tabellen; Korrelation DB-Fallback + atomares Resolve; Gateway-Retry → `messaging_outbox`, vom Leader abgearbeitet | `notification_service.py`, `correlation.py:141-172`, `gateway.py:54-120` |
| Plugin-Artefakte | Reconciler je Instanz: `plugin_states` (desired_version) → lokaler Download/Install; Marketplace-Install schreibt nur DB + `plugin:desired_changed`; Leader macht Collection-Sync/Update-Check | `plugin_service.py`, `manager.py:921-977` |
| Restart | HA-Modus (`LYNDRIX_CLUSTER=1`): `/api/system/restart` → `system:restart_requested` → geordnetes Beenden je Instanz gestaffelt (Leader zuletzt); kein Docker-Socket nötig | `main.py:556-595` |
| Platzierung | `placement` im Manifest, `LYNDRIX_INSTANCE_ROLES`, `pinned`-Gate im Router-Registry | `plugins/logic/models.py`, `router_registry.py` |
| Diverses | Logs → stdout (Datei optional), i18n-Version aus Inhalts-Hash, `/metrics` (Prometheus, Label `instance`), Theme-Uploads/Backgrounds → `ctx.storage` (Local/S3) | `core/logger.py`, `core/i18n.py:130-153`, `themes_api.py`, `auth_api.py:629` |

Versionen: `__api_version__` 1.6.0, `app/version.py` 1.4.0. Tests: `tests/cluster/` (Lease-Erwerb,
Verlust, Fan-out, kv-TTL) auf SQLite + ein **Zwei-Instanzen-Dev-Compose**
(`lyndrix-core-dev` + `lyndrix-core-dev-2`, gemeinsame DB/Vault, `docker/traefik.dev.yml` existiert
bereits als LB) als Pflicht-Prüfstand für jede HA-Änderung.

---

## 10. Umsetzungsplan

| Phase | Ziel | Pakete | Repos | Aufwand |
|---|---|---|---|---|
| **0 — Überleben** (sofort, auch ohne HA sinnvoll) | heutige Einzelinstanz robust + wiederherstellbar | Backups (`mariadb-dump`, Vault-Snapshot, Restore-Übung); `/api/live` + `/api/ready` + Compose-Healthcheck + Traefik-Health; `--forwarded-allow-ips` (Rate-Limiter-Bug heute); Boot-Races idempotent + `GET_LOCK`; DB-Loop gibt nie auf, kein Re-Bootstrap; Vault-Health-Poll + `vault:status_changed`; OIDC-Callback aus NiceGUI-Gate; Notifications → DB; Logs stdout | core, SSoT `aac-lyndrix-core` | M |
| **1 — Core cluster-fähig** | zwei API-Instanzen hinter dem Dev-Traefik verhalten sich wie eine | §9.4 komplett, Zwei-Instanzen-Dev-Compose, Shell: Runtime-Status mit `instance_id` aggregieren | core, ui | L |
| **2 — Plugins** | kein Plugin läuft N-fach, keiner braucht lokale Platte außer Worker | monitoring (Lease-Prober, UNIQUE + atomare Zähler, Cluster-Resync); orchestrator (`pinned:runner`, `owner_instance`, DB-Reservierung, Trigger via Cluster-Event, Job-Logs in DB/Shared); ssot-manager (ablösen/pinnen); docker-manager (DB-Tabelle); server-manager/registry/itsm (`IF NOT EXISTS`, Retry, `UNIQUE(fingerprint)`); itsm Anhänge → `ctx.storage`; discord Store → DB; external-services (Sidebar aus DB, kein DDL in `health()`); bingo (DB) | alle Plugins | L (Orchestrator allein M–L) |
| **3 — Daten & Secrets HA** | Galera ×3 + MaxScale, Vault/OpenBao Raft ×3, Object-Store | neue SSoTs `aac-lyndrix-db` (Galera je Host), `aac-maxscale`, `aac-lyndrix-vault` (Raft je Host); Core `VAULT_NODES`/`DB_HOST`→MaxScale; Migration Dump→Galera, `operator migrate` file→raft; Backups umstellen | iac-environment, core | M–L |
| **4 — Deployment-Profil** | N Instanzen produktiv, Rolling Update, Chaos-Tests | Profil A oder B (§11); keepalived-VIP (A); Rollout-Reihenfolge (Migrationsjob → Worker → API-Instanzen einzeln); Tests: Instanz killen, DB-Knoten killen, Vault-Knoten sealen, Hypervisor stoppen; Prometheus-Scrape + Grafana-Dashboard | iac-environment | M |

Kritischer Pfad: 0 → 1 → 2 (Monitoring + Orchestrator) → 3 → 4. Phase 3 kann parallel zu 2
vorbereitet werden (Infra), braucht aber Phase 1 (Retry/Reconnect/`VAULT_NODES`) vor dem Umschalten.

**Verifikation je Phase**

- 0: `docker stop lyndrix-db` → Core bleibt am Leben, `/api/ready` 503, nach `start` wieder 200
  ohne Re-Bootstrap; Restore-Übung aus Dump auf `docker-test` dokumentiert.
- 1: zwei Instanzen, Plugin-Install auf A sichtbar auf B (SSE + Router + Bundle); Theme-Wechsel
  propagiert ≤ 2 s; Leader-Kill → neue Lease ≤ 30 s; `/api/health` konsistent; OIDC-Login mit
  erzwungenem Instanzwechsel zwischen Callback und Exchange.
- 2: Heartbeat-Rate bei 2 Instanzen = bei 1; `monitoring_daily_aggregates` ohne Duplikate;
  Orchestrator-Job läuft weiter, wenn eine API-Instanz stirbt; API-Instanz-Restart FAILt keinen Job.
- 3: `kill -9` auf Galera-Writer → MaxScale wählt neuen Writer, Core-Requests ≤ 1 Retry; Vault-
  Knoten `seal` → Leader entsiegelt ≤ 20 s; `sys/health` je Knoten im `/api/health`.
- 4: Hypervisor hydra stoppen → `lyndrix.int` antwortet weiter; Rolling Update ohne 5xx am LB.

---

## 11. Deployment-Profile

**Standort-Vorentscheidung:** Galera/Raft laufen an **einem** Standort. Onprem Olympus hat seit
2026-10-12 **drei** PVE-Nodes (hydra, cerberus, chimera) → echte 3-fach-Platzierung; Hetzner titan
hat zwei (prox1, cronus) → 3 Knoten bedeuten zwei auf einem Hypervisor (Quorum-Verlust bei dessen
Ausfall; das gilt heute auch für die k3s-Master). Lyndrix verwaltet primär die Onprem-Infra
(Orchestrator → Proxmox, Docker-Hosts); läuft es in Hetzner, hängt Onprem-Verwaltung am WAN-Link.
→ **Empfehlung: Onprem Olympus.**

### Profil A — Docker Compose × 3 Nodes (empfohlener erster Schritt)

- Drei Docker-VMs/LXCs, eine je PVE-Node: `docker-cerberus` (.61), `docker-hydra` (.62), neu
  `docker-chimera`. Chimera ist ein Laptop → ggf. nur Galera-Arbiter (`garbd`) + Vault-Knoten +
  eine API-Instanz, kein Worker.
- SSoT-Umbau: `aac-lyndrix-core` verliert die `dependencies:` DB/Vault; stattdessen eigene SSoTs
  `aac-lyndrix-db` (Galera-Node, je Host ein Eintrag — die Engine erlaubt dieselbe SSoT auf
  mehreren Hosts als unabhängige Stacks, genau das passt), `aac-maxscale` (je Host, lokal),
  `aac-lyndrix-vault` (Raft-Node je Host). `aac-lyndrix-core` auf allen drei Hosts mit
  `LYNDRIX_INSTANCE_ROLES=api` (+ `runner` auf dem heutigen `docker-auto`-Nachfolger, der den
  Docker-Socket und `/export/docker/...`-Pfade behält).
- **VIP:** neue Rolle/SSoT `keepalived` (VRRP) auf den drei Hosts, `lyndrix.int` → VIP; der
  per-Host-Traefik auf dem VIP-Halter terminiert TLS und balanciert auf die drei Core-Instanzen
  (Traefik-File-Provider mit Health `/api/ready`). Alternative ohne neue Technik: PowerDNS
  Round-Robin — Browser-Failover ist langsam, nur Übergangslösung.
- Passt zur bestehenden Pipeline (`cd_rollout` ist host-scoped → Rolling Update = Host für Host).
- Grenzen: kein Scheduler, Platzierung ist Handarbeit im `hosts.yml`; Compose kennt keine
  Readiness-gesteuerte Verkehrssteuerung → Traefik-Health muss das leisten.

### Profil B — k3s

- Vorhanden: 3 Master, Longhorn, MetalLB, Traefik, cert-manager, Fleet. Lyndrix-Objekte:
  `Deployment lyndrix-core` (replicas 2–3, `readinessProbe /api/ready`, `livenessProbe /api/live`,
  `LYNDRIX_UI_ENGINE=react`, Plugins im Image oder Init-Container-Reconcile), `Deployment
  lyndrix-ui`, `Job lyndrix-migrate` (pre-upgrade), `mariadb-operator` (Galera + MaxScale, Backups
  als CRD), Vault/OpenBao Helm-Chart (Raft ×3, Longhorn), Worker als separates `Deployment` mit
  `replicas: 1` + `nodeSelector` auf einen Node mit Docker (oder DinD) — bis Runner zu k8s-Jobs
  werden (Redesign des Orchestrators, nicht Teil dieses Plans).
- Gewinn: Rolling Updates, Readiness-Routing, Operator-gestützte DB, GitOps mit Fleet.
- Hürden: Cluster steht in Hetzner (Standortfrage oben); `TE/templates/kubernetes/` ist
  ungetestet; Orchestrator-Runner brauchen Docker; kein Lyndrix-Workload heute auf k3s.
- Sinnvoll als **Zielbild nach Profil A**, oder sofort, wenn ein Onprem-k3s entsteht
  (`aac-k3s-init` als Vorlage).

---

## 12. Offene Entscheidungen

1. **Standort** des HA-Clusters: Onprem Olympus (empfohlen) oder Hetzner k3s?
2. **Profil A (Compose ×3 + keepalived) zuerst** oder direkt k3s?
3. **Koordinations-Treiber:** DB-only (empfohlen für Phase 1) — Redis später nur bei Bedarf
   (Latenz, NiceGUI-Storage)?
4. **Vault oder OpenBao** im Lyndrix-Stack (Lizenz, Fleet-Konsistenz)? Transit-Auto-Unseal gegen
   zentrales OpenBao oder Unseal durch den Lyndrix-Leader (empfohlen, keine WAN-Abhängigkeit)?
5. **`pinned`-Semantik** für API-Instanzen: 503 „remote“ (einfach) oder Reverse-Proxy zum Worker
   (nahtlos, mehr Code)?
6. **NiceGUI-Ende** im HA-Betrieb bestätigen → aac-ssot-manager ablösen statt pinnen?
7. **Object-Store:** MinIO (neu onprem oder bestehend auf `cicd-core`) vs. TrueNAS-NFS-Volume?
8. **Migrationen:** `GET_LOCK` um den heutigen `create_all`/`ALTER`-Pfad (klein) oder Alembic +
   Init-Job (sauber, mehr Arbeit, Pflicht für Galera-TOI)?
9. **Monitoring-Prober:** Lease (ein Prober) reicht für ~160 Monitore; Sharding erst bei > 1 000?
10. **Chimera-Rolle:** Vollknoten (API + Galera + Vault) oder nur Arbiter?

---

## 13. Quellen

Drei Lese-Erhebungen am 2026-10-14 (keine Dateien geändert, keine Secrets ausgegeben):
Core-Zustandsinventar (`lyndrix-core/app`, Core 1.3.0), Plugin-Inventar (10 Plugins unter
`lyndrix-dev/plugins/`), Deployment-Inventar (`iac-environment`: `aac-lyndrix-core`,
`iac-controller`, `aac-template-engine`, `aac-k3s-init`, `iac-audit`-Berichte vom 2026-09-22/25).
Vorarbeit: `features/rbac-security-k8s-audit.md` Teil C (2026-07-05).
