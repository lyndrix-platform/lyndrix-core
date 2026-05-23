# Installation & Deployment

## Aktueller Lieferumfang

Im Repository ist **eine vollständige Development-Deployment-Strecke** enthalten:

- `docker/docker-compose.dev.yml`
- `docker/Dockerfile`
- `docker/.env.dev`

Diese Umgebung startet:

- Lyndrix App (`lyndrix`)
- MariaDB (`db`)
- HashiCorp Vault (`vault`)
- Doku-Server (`docs` via zensical)

## Development Setup

```bash
git clone https://github.com/lyndrix-platform/lyndrix-core.git
cd lyndrix-core

docker compose -f docker/docker-compose.dev.yml up -d --build
```

Endpoints:

- App: `http://localhost:8081`
- Vault UI: `http://localhost:8200`
- Docs: `http://localhost:8000`

## Persistenzpfade

`docker-compose.dev.yml` bindet lokale Persistenz unter `.dev/`:

- `../.dev/storage:/data/storage`
- `../.dev/secure_data:/data/security`
- `../.dev/db_data:/var/lib/mysql`
- `../.dev/vault_data:/vault/file`

## Plugin-Mounts im Dev-Compose

Die Dev-Compose-Datei enthält optionale Bind-Mounts für externe Plugin-Repositories (z. B. lokale Entwicklung mehrerer Repos). Diese Pfade müssen auf deinem Host existieren oder angepasst werden.

## Produktionsbetrieb

Für produktive Umgebungen gilt:

- eigenes Compose/Kubernetes-Manifest aus dem Dev-Stack ableiten
- `--reload` deaktivieren
- starke Secrets setzen (DB + Storage + Vault)
- `LYNDRIX_MASTER_KEY` nicht unkontrolliert in Produktivsystemen verwenden
- TLS/Ingress/Reverse-Proxy vorschalten
- Backups für:
  - MariaDB-Daten
  - Vault Storage
  - `vault_keys.enc` (Pfad: `settings.LYNDRIX_VAULT_KEY_FILE`)

## Betriebs-Checks

```bash
# Containerstatus
docker compose -f docker/docker-compose.dev.yml ps

# Logs
docker compose -f docker/docker-compose.dev.yml logs -f lyndrix

# Health (Root erreichbar)
curl -f http://localhost:8081/
```

## Update-Strategie (Empfehlung)

1. Backup von DB + Vault + `vault_keys.enc`
2. Neue Version ausrollen
3. Logs auf Boot-Phasen prüfen (`system:boot_phase`)
4. Plugin-Status im Plugin-Manager prüfen
