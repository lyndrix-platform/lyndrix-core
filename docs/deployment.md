# Installation & Deployment

## Deployment-Modelle im Lyndrix-Setup

In eurer Umgebung gibt es bewusst **zwei getrennte Wege**:

- **Local Development** direkt aus diesem Repository
- **Production Deployment** als generierte Docker-Compose-Datei aus einer externen `service.yml`

Damit ist klar: Die Dev-Dateien in diesem Repo sind nicht die Produktionsquelle.

## 1) Local Development (Repository-basiert)

Die lokale Entwicklungsstrecke liegt vollständig im Repo:

- `docker/docker-compose.dev.yml`
- `docker/Dockerfile`
- `docker/.env.dev`
- `docker/entrypoint.sh`

Diese Umgebung startet:

- Lyndrix App (`lyndrix`)
- MariaDB (`db`)
- HashiCorp Vault (`vault`)
- Doku-Server (`docs`)

Start:

```bash
git clone https://github.com/lyndrix-platform/lyndrix-core.git
cd lyndrix-core
docker compose -f docker/docker-compose.dev.yml up -d --build
```

Endpoints:

- App: `http://localhost:8081`
- Vault UI: `http://localhost:8200`
- Docs: `http://localhost:8000`

### Wichtige Dev-Eigenschaften

- App läuft mit Live-Entwicklung (`uvicorn ... --reload`)
- Source-Code und Plugin-Repositories sind als Bind-Mounts eingebunden
- Persistenz liegt unter lokalen `.dev/`-Pfaden

Persistenz-Mounts (Auszug):

- `../.dev/storage:/data/storage`
- `../.dev/secure_data:/data/security`
- `../.dev/db_data:/var/lib/mysql`
- `../.dev/vault_data:/vault/file`

## 2) Production Deployment (IaC-generiert)

Die produktive Compose-Datei wird **nicht** manuell aus dem Dev-Compose gepflegt, sondern vom IaC-Orchestrator aus einer Service-Definition erzeugt.

Quelle (Definition):

- `aac-application-defenitions/applications/aac-lyndrix-core/service.yml`

Ziel (generiertes Artifact, Beispielpfad auf dem Host):

- `../docker/aac-lyndrix-core/compose/docker-compose.yml`

### Was aus der `service.yml` in der generierten Compose ankommt

- App-Image inkl. Tag (z. B. `ghcr.io/lyndrix-platform/lyndrix-core:0.0.8`)
- Service-/Container-Name und Hostname
- Ports (`8081`)
- Persistente Host-Volumes unter `/export/docker/aac-lyndrix-core/...`
- Abhängigkeiten als Sidecars (`aac-lyndrix-db`, `aac-lyndrix-vault`)
- `depends_on`-Bedingungen (DB healthy, Vault started)
- Traefik-, Auto-DNS- und Homepage-Labels
- Netzwerkzuordnung (`secured`, `exposed`, `interconnect`, `stack_internal`)

### Production-Charakteristika

- Gepinntes Container-Image statt lokalem Build
- Kein Dev-Reload-Workflow
- Externe Netzwerke und Ingress-Routing über Traefik
- Persistenz auf stabilen Host-Pfaden

## 3) Schnellvergleich: Dev vs Prod

- **Source of truth**:
  - Dev: Dateien im Repo
  - Prod: `service.yml` + IaC-Orchestrator
- **Runtime**:
  - Dev: lokaler Build + Live-Mounts
  - Prod: gepinntes Image
- **Storage**:
  - Dev: `./.dev/*`
  - Prod: `/export/docker/aac-lyndrix-core/*`
- **Networking**:
  - Dev: localhost-orientiert
  - Prod: Traefik + DNS + externe Netzwerke

## 4) Betriebs-Checks

### Dev-Checks

```bash
docker compose -f docker/docker-compose.dev.yml ps
docker compose -f docker/docker-compose.dev.yml logs -f lyndrix
curl -f http://localhost:8081/
```

### Prod-Checks (im generierten Compose-Verzeichnis)

```bash
docker compose ps
docker compose logs -f aac-lyndrix-core
docker compose logs -f aac-lyndrix-db
docker compose logs -f aac-lyndrix-vault
curl -fk https://lyndrix.int.fam-feser.de/
```

## 5) Sicherheits- und Update-Hinweise

- Secrets niemals im Klartext versionieren
- Vor Updates immer Backup von DB, Vault-Daten und `vault_keys.enc` erstellen
- Nach Rollout Boot-Phasen und Plugin-Status prüfen
- `LYNDRIX_MASTER_KEY` in Prod nur kontrolliert verwenden

Empfohlene Rollout-Reihenfolge:

1. Backup erstellen
2. Neues Image/Tag über `service.yml` setzen
3. Compose neu generieren und ausrollen
4. Health/Logs/Plugin-Status verifizieren
