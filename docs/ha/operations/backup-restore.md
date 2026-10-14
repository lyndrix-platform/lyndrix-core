# Operations — backup and restore

HA is not a backup. A deployment that was never restored from a backup has no backup. This page
is Tier 0 — do it before anything else in this section.

## What to back up

| Data | Where | How |
|---|---|---|
| Database (users, groups, plugin state, monitors, ITSM records, cluster tables) | MariaDB | `mariadb-dump --single-transaction --routines --events lyndrix_db` nightly; or `mariabackup` |
| Secrets and settings | Vault / OpenBao | `vault operator raft snapshot save` (Raft) or stop + tar of the file storage (file backend) |
| Unseal material | `SECURITY_DIR/vault_keys.enc` + `LYNDRIX_MASTER_KEY` | copy the key file; keep the master key in your password manager — without both the Vault data is unreadable |
| Shared files | `/data/storage` | file-level backup of the mount |
| Plugin artifacts | `/app/plugins` | optional — they are re-installable from `plugin_states` (`repo_url`, `desired_version`) |

The `cluster_*` tables need no special handling; stale rows are pruned after a restore
(instances re-register on their next heartbeat).

## Restore sequence

1. Start Vault/OpenBao, restore the snapshot (or the file storage), unseal with the key file +
   master key (the core does this on boot when both are present).
2. Restore the database dump into an empty `lyndrix_db`.
3. Mount `/data/storage` from backup.
4. Start **one** core instance in single mode first, log in, check Settings → Plugins (plugins
   re-install if the folder is missing) and the monitors/records.
5. Switch to cluster mode and start the other instances.

## Rehearse

Do the restore on a scratch host once per quarter and note the time it took: that is your real
RTO. The dumps' age is your RPO.
