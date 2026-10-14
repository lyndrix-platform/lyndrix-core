# Building block: secrets (Vault / OpenBao)

## Contract

- One endpoint (`VAULT_URL`) that answers KV v2 reads and writes for the `lyndrix` mount.
- The unseal material: `LYNDRIX_MASTER_KEY` plus the encrypted key file
  `SECURITY_DIR/vault_keys.enc` (Argon2 + AES-GCM). In HA both are shared secrets present on every
  instance — the same security level as today (both lived on one VM).
- Standby nodes may forward requests to the active node; sealed nodes must be out of rotation.

## Options

| Option | HA of the layer | Unseal | Notes | When |
|---|---|---|---|---|
| Single Vault, file storage | none | by the core (`LYNDRIX_MASTER_KEY` + key file) | dev stack, Tier 1 | Tier 0/1 |
| **Vault or OpenBao, Raft × 3** | yes | today: each node by Vault's own auto-unseal (Transit/KMS) or manually; planned: by the Lyndrix leader | `vault operator migrate` file → raft works for both products; OpenBao is MPL-licensed and API-compatible | Tier 2 |
| External Vault/OpenBao you already run | theirs | theirs | `VAULT_URL` to it; `lyndrix` mount is created on first start | any |
| Transit auto-unseal against a central Vault | depends on that Vault | automatic | adds a WAN dependency if the central Vault is remote | when it is HA itself |

Health semantics for a load balancer in front of Vault nodes: `/v1/sys/health` returns
200 active, 429 standby (healthy for reads via forwarding), 503 sealed — treat 200 and 429 as up.

## Lyndrix settings

`VAULT_URL`, `VAULT_NODES` (every node; the preflight probes `/v1/sys/health` on each),
`VAULT_SKIP_VERIFY` (dev only), `LYNDRIX_MASTER_KEY`.

## Reference setup

Not shipped as files yet. Outline: three `vault`/`openbao` containers with
`storage "raft" { path = "/vault/data"; retry_join { leader_api_addr = "http://v1:8200" } … }`,
`api_addr`/`cluster_addr` per node; migrate the existing file storage with
`vault operator migrate -config migrate.hcl` on node 1, then join 2 and 3. The existing key file
stays valid (same unseal key).

## Verify

```bash
# preflight "vault_nodes" ok: every node in VAULT_NODES answers
vault operator seal   # on one node → preflight warn; app keeps working through the others
```

## Known limits

- The core unseals only the node behind `VAULT_URL`. Other nodes need Vault-side auto-unseal
  until `VAULT_UNSEAL_MODE=lyndrix` (leader unseals every node in `VAULT_NODES`) ships.
- Root token from the key file; AppRole (`VAULT_AUTH=approle`) is planned.
- Settings saved on one instance propagate via `settings:changed` (peers re-read Vault); Vault
  KV writes are still read-modify-write without check-and-set.
