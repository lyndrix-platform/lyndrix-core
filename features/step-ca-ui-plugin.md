# Feature idea: `lyndrix-plugin-pki` — web UI for step-ca

**Status:** 💡 Idea / Planned (no design yet, no code)
**Requested:** Marvin, 2026-11-12
**Affected repos (new):** `lyndrix-plugin-pki` (working title)

---

## 1. Motivation

Since 2026-11-09 a private CA on `step-ca` (0.30.x, ACME provisioner
for Traefik, root CA valid for 10 years) has been running in the homelab. step-ca itself has **no web UI** — everything runs through the
`step` CLI and the HTTPS API. A UI only exists in the commercial Smallstep Certificate Manager.

Open-source UIs reviewed (2026-11-11) do not fit:

| Project | Reason against adoption |
|---|---|
| `UncleFi1/step-ca-ui` (Go, GPL-3.0) | Very small project, own user management + 3 containers, no SSO |
| `marcin-kruszynski/step-ui` (Next.js+Go, MIT) | No auth, own SQLite inventory, very early stage |
| `damhau/stepca-web` (Flask) | Requires step-ca with PostgreSQL backend (we use Badger) |

Lyndrix already has auth/RBAC (OIDC), Vault, entity registry, notifications and ITSM — a PKI
UI as a plugin reuses all of that instead of setting up an island next to it.

## 2. Rough feature scope (basis for discussion)

- **Overview:** root/intermediate (fingerprint, runtime), provisioner list, CA health.
- **Certificates:** list issued certificates incl. ACME orders, expiry traffic light,
  revocation (revoke), manual issuance/CSR signing via JWK provisioner.
- **Expiry monitoring:** notification/ITSM task before expiry, connection to `state_monitoring`.
- **Entity registry:** link certificate ↔ service/host CI (which Traefik router uses what).
- **Root distribution:** download root CA (PEM/DER) + status of the trust rollout on the hosts.
- **RBAC:** roles viewer / operator (issue) / admin (revoke, provisioners).

## 3. Open questions

- Data source: step-ca admin API (remote provisioner management must be enabled) vs.
  own inventory via ACME/Traefik storage vs. Badger DB read-only?
- Provisioner credentials in Vault; rotation is **not** part of the plugin.
- One plugin for multiple CAs?
- `placement`: CA communication from the runner or from the control plane?

## 4. Non-goals

- No replacement for step-ca, no CA keys in the plugin.
- No secret rotation/migration.
