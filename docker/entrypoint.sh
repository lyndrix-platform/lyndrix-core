#!/bin/bash
set -e

echo "[Boot] Lyndrix Entrypoint wird ausgeführt..."

# 1. Plugins wiederherstellen, falls das gemountete Volume leer ist
if [ ! "$(ls -A /app/plugins 2>/dev/null)" ]; then
    if [ -d "/opt/lyndrix/plugins_backup" ] && [ "$(ls -A /opt/lyndrix/plugins_backup)" ]; then
        echo "[Boot] /app/plugins ist leer. Kopiere Standard-Komponenten..."
        cp -r /opt/lyndrix/plugins_backup/. /app/plugins/
    fi
fi

# 2. Daten-Verzeichnisse vorbereiten
#    SC-01: no world-writable modes. /data/security holds the encrypted Vault key
#    blob (root token + unseal key) and /app/plugins is code the core imports as
#    root — a recursive 777 handed both to every local user / co-mounted container.
#    The core runs as root, so it needs no group/other access to either tree.
mkdir -p /data/security
chmod 700 /data/security 2>/dev/null || true
if [ -f /data/security/vault_keys.enc ]; then
    chmod 600 /data/security/vault_keys.enc 2>/dev/null || true
fi
#    Undo the world-write bit earlier releases set on the plugin tree (owner and
#    group bits stay as they are, so bind-mounted developer checkouts keep working).
chmod -R o-w /app/plugins 2>/dev/null || true
# TODO(agent): run as a non-root USER. Blocked on the docker socket (runner spawn,
# self-restart) and on existing root-owned bind mounts — needs a migration plan.

# 3. Reverse-Proxy-Vertrauen: uvicorn liest FORWARDED_ALLOW_IPS nativ (--forwarded-allow-ips).
#    Ohne diesen Wert sieht der Core hinter einem Load Balancer nur dessen IP
#    (Login-Limiter, Audit). Quelle ist LYNDRIX_TRUSTED_PROXIES (docs/ha/configuration.md).
export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-${LYNDRIX_TRUSTED_PROXIES:-127.0.0.1}}"

echo "[Boot] Starte Anwendung: $@"
exec "$@"