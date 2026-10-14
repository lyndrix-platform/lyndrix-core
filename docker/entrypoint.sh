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
mkdir -p /data/security
chmod -R 777 /app/plugins /data/security 2>/dev/null || true

# 3. Reverse-Proxy-Vertrauen: uvicorn liest FORWARDED_ALLOW_IPS nativ (--forwarded-allow-ips).
#    Ohne diesen Wert sieht der Core hinter einem Load Balancer nur dessen IP
#    (Login-Limiter, Audit). Quelle ist LYNDRIX_TRUSTED_PROXIES (docs/ha/configuration.md).
export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-${LYNDRIX_TRUSTED_PROXIES:-127.0.0.1}}"

echo "[Boot] Starte Anwendung: $@"
exec "$@"