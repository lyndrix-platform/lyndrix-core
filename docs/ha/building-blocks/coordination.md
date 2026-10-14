# Building block: coordination

The coordination layer answers four questions for the instances of one deployment: who is alive,
who holds which lease, what is the shared short-lived state, and what happened elsewhere.

## Contract

- Atomic acquire/renew of named leases with a TTL, compared on a clock every instance shares.
- A TTL key/value store (OIDC state, SSO codes, rate-limiter counters).
- Append-only event fan-out with "skip my own events" semantics and no replay for late joiners.
- Instance registry with heartbeats.

## Options

| Driver | Where it lives | Extra dependency | Fan-out latency | When |
|---|---|---|---|---|
| `local` | process memory | none | — | single mode (default); every lease granted to this instance |
| `db` | the shared MariaDB: `cluster_instances`, `cluster_leases`, `cluster_kv`, `cluster_events` | none — the DB is already there and already HA in Tier 2 | poll interval (500 ms) | cluster mode (default) |
| `redis` | Redis / Valkey | a Redis that itself must be HA (Sentinel/Cluster) | sub-100 ms | planned — not shipped |

Why the database first: HA must not introduce a new mandatory dependency that itself needs to be
made highly available. Lyndrix's event rate is small (runtime status coalesced to 1/s,
notifications, plugin state changes), so polling is cheap.

## Lyndrix settings

`LYNDRIX_COORDINATOR`, `LYNDRIX_LEASE_TTL_SECONDS`, `LYNDRIX_LEASE_HEARTBEAT_SECONDS`,
`LYNDRIX_EVENT_POLL_MS`, `LYNDRIX_INSTANCE_ID`, `LYNDRIX_INSTANCE_ROLES`.

## How the `db` driver behaves

- Tables are created once under a cross-instance advisory lock (`GET_LOCK('lyndrix_bootstrap')`);
  a lost race (error 1050) is tolerated and the start is retried with back-off.
- Every timestamp comes from the database (`UTC_TIMESTAMP()`); instance clocks are never compared.
- Transactions run **READ COMMITTED** and retry MariaDB errors 1020 (snapshot isolation: "record
  has changed since last read"), 1205 and 1213 — two instances renewing a lease at the same moment
  is exactly the race that produces 1020 under MariaDB ≥ 11.6 defaults.
- A fresh instance starts reading events at `MAX(id)`; the leader prunes events older than an
  hour, dead instance rows and expired keys every five minutes.
- An instance that stops gracefully deletes its row and its leases and broadcasts
  `cluster:lease_released`, so peers acquire immediately.

## Verify

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://lyndrix.example.com/api/cluster | jq '.leader, .alive, .lease_table'
docker stop <leader instance>        # another instance holds core:leader within ~1 s (graceful) / ≤ TTL (killed)
```

Core unit tests: `tests/cluster/test_db_coordinator.py` (two coordinators on one SQLite engine),
`tests/cluster/test_lease_keeper.py`, `tests/cluster/test_bridge.py`.

## Known limits

- `redis` driver not shipped.
- Fan-out latency equals the poll interval (500 ms default) — fine for UI refreshes and plugin
  state, not meant for high-frequency data.
