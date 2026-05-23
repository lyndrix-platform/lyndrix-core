# Security & Vault

## Security-Architektur im Core

Lyndrix Core verfolgt ein Vault-zentriertes Sicherheitsmodell:

- Secrets liegen in HashiCorp Vault (`lyndrix/` KV v2)
- Vault-Schlüsselmaterial wird **verschlüsselt** in `vault_keys.enc` abgelegt
- Ohne gültigen Master-Key ist Entschlüsselung nicht möglich

## Verschlüsselung von `vault_keys.enc`

Implementierung: `app/core/components/vault/logic/crypto.py`

- Key-Derivation: **Argon2id** (`hash_secret_raw`)
- Symmetrische Verschlüsselung: **AES-GCM**
- Blob-Layout:
  - `salt[16] + nonce[16] + tag[16] + ciphertext[n]`

Konfigurierbare Parameter (via `app/config.py`):

- `LYNDRIX_ARGON_TIME`
- `LYNDRIX_ARGON_MEM`
- `LYNDRIX_ARGON_PARALLEL`

## Vault-Lifecycle

1. `system:started`
2. Vault-Healthcheck
3. Falls uninitialisiert: `vault:needs_init`
4. Falls sealed: `vault:needs_unseal`
5. Nach Erfolg: `vault:opened` und `vault:ready_for_data`

Auto-Flow:

- Ist `LYNDRIX_MASTER_KEY` gesetzt, kann Auto-Init/Auto-Unseal ausgelöst werden.

## Plugin-Secret-Isolation

Plugins nutzen `ModuleContext`:

- `ctx.get_secret(key)`
- `ctx.set_secret(key, value)`

Der Core trennt Pfade pro Modul (`plugins/<manifest.id>`). Damit teilen sich Plugins nicht automatisch denselben Secret-Bereich.

## DB- und Betriebs-Sicherheit

`DatabaseService`:

- nutzt `pool_pre_ping` für robuste Verbindungen
- unterscheidet permanente Konfigurationsfehler von transienten Verbindungsfehlern
- emittet bei Problemen `system:maintenance_mode`

## Härtungsempfehlungen

- Produktiv: starke Werte für `DB_PASSWORD`, `STORAGE_SECRET`, Admin-Credentials
- `LYNDRIX_MASTER_KEY` sicher verwalten (Secrets Manager/HSM)
- Vault UI und App nur über TLS und abgesicherte Netze exponieren
- Regelmäßige Backups + Restore-Tests
- Logs auf Sensitive Data prüfen (Secrets niemals loggen)

## Security-relevante Events (Auszug)

- `vault:init_requested`
- `vault:unseal_requested`
- `vault:auth_failed`
- `system:maintenance_mode`
- `db:connected`
