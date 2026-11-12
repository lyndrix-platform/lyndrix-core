# Feature-Idee: `lyndrix-plugin-pki` — Web-UI für step-ca

**Status:** 💡 Idee / Planned (noch kein Design, kein Code)
**Angefragt:** Marvin, 2026-11-12
**Betroffene Repos (neu):** `lyndrix-plugin-pki` (Arbeitstitel)

---

## 1. Motivation

Seit 2026-11-09 läuft im Homelab eine private CA auf `step-ca` (0.30.x, ACME-Provisioner
für Traefik, Root-CA 10 Jahre). step-ca selbst hat **keine Web-UI** — alles läuft über die
`step`-CLI und die HTTPS-API. Eine UI gibt es nur im kommerziellen Smallstep Certificate Manager.

Geprüfte Open-Source-UIs (2026-11-11) passen nicht:

| Projekt | Grund gegen Übernahme |
|---|---|
| `UncleFi1/step-ca-ui` (Go, GPL-3.0) | Kleinstprojekt, eigene User-Verwaltung + 3 Container, kein SSO |
| `marcin-kruszynski/step-ui` (Next.js+Go, MIT) | ohne Auth, eigene SQLite-Inventarisierung, sehr früh |
| `damhau/stepca-web` (Flask) | setzt step-ca mit PostgreSQL-Backend voraus (wir: Badger) |

Lyndrix hat bereits Auth/RBAC (OIDC), Vault, Entity-Registry, Notifications und ITSM — eine
PKI-UI als Plugin nutzt das alles mit, statt eine Insel daneben zu stellen.

## 2. Grober Funktionsumfang (Diskussionsgrundlage)

- **Übersicht:** Root/Intermediate (Fingerprint, Laufzeit), Provisioner-Liste, CA-Health.
- **Zertifikate:** ausgestellte Zertifikate inkl. ACME-Orders listen, Ablauf-Ampel,
  Widerruf (revoke), manuelle Ausstellung/CSR-Signierung über JWK-Provisioner.
- **Ablauf-Monitoring:** Notification/ITSM-Task vor Ablauf, Anbindung an `state_monitoring`.
- **Entity-Registry:** Zertifikat ↔ Service-/Host-CI verknüpfen (welcher Traefik-Router nutzt was).
- **Root-Verteilung:** Download Root-CA (PEM/DER) + Status des Trust-Rollouts auf den Hosts.
- **RBAC:** Rollen viewer / operator (ausstellen) / admin (revoke, Provisioner).

## 3. Offene Fragen

- Datenquelle: step-ca-Admin-API (Remote-Provisioner-Management muss aktiv sein) vs.
  eigene Inventarisierung über ACME/Traefik-Storage vs. Badger-DB read-only?
- Provisioner-Credentials in Vault; Rotation ist **nicht** Teil des Plugins.
- Ein Plugin für mehrere CAs?
- `placement`: CA-Kommunikation vom Runner oder von der Control-Plane?

## 4. Nicht-Ziele

- Kein Ersatz für step-ca, keine CA-Schlüssel im Plugin.
- Keine Secret-Rotation/-Migration.
