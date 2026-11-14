# Building block: database

## Contract

- **One writer.** The data model uses `SELECT … FOR UPDATE`, advisory locks (`GET_LOCK`) and
  read-modify-write on JSON columns. All sessions must land on the same node; a proxy that fails
  over to another node is fine, a multi-writer setup is not.
- Every table has a primary key (Galera requirement; true for the core and the shipped plugins).
- Connections may be dropped by a proxy — the core recycles pooled connections
  (`DB_POOL_RECYCLE_SECONDS`) and reconnects forever after the first successful connect without
  re-running its bootstrap.
- DDL happens at boot, serialized across instances by `GET_LOCK('lyndrix_bootstrap')`.

## Options

| Option | HA of the layer | Effort | Notes | When |
|---|---|---|---|---|
| Single MariaDB | none | — | what the dev stack and Tier 1 use | Tier 0/1 |
| **Galera × 3 + MaxScale** (`readwritesplit`, one writer) | automatic writer failover in seconds, synchronous replication | medium | one MaxScale per core host, addressed locally → no extra SPOF; `wsrep_sync_wait` only if reads ever leave the writer | Tier 2 on Docker |
| Galera × 3 + ProxySQL | same | medium | alternative proxy | Tier 2 |
| Primary + semi-sync replica + MaxScale `mariadbmon` | failover automatic, rejoin manual | lower | async risk window | two-host setups |
| mariadb-operator (`MariaDB` CRD with `galera:` + `MaxScale` CRD) | yes | low (declarative) | includes `Backup` CRD and rolling updates | Kubernetes |
| Managed MariaDB/MySQL | provider | low | check `innodb_snapshot_isolation` and `max_statement_time` support | cloud |

## Lyndrix settings

`DB_HOST` (writer endpoint), `DB_POOL_RECYCLE_SECONDS`, `DB_STATEMENT_TIMEOUT_SECONDS`,
`DB_SOCKET_TIMEOUT_SECONDS`, `DB_ALLOW_DIRECT_NODE`.

### Dead peers (core ≥ 1.11.1)

A pooled connection whose peer vanished without a FIN/RST (the DB VIP moved to another node, a
NAT/conntrack entry expired, a half-open TCP session) does not fail — the kernel retransmits for
`tcp_retries2` = 15 rounds, about 16 minutes, and every caller of that connection waits, including
`pool_pre_ping`, the cluster heartbeat and lease renewals. The core therefore sets pymysql
`read_timeout`/`write_timeout` (`DB_SOCKET_TIMEOUT_SECONDS`, auto = statement cap + 30 s): the query
fails with error 2013, SQLAlchemy drops the connection and the next call opens a fresh one. On top of
that the heartbeat and each lease renewal are abandoned after the lease TTL, so an instance logs the
outage within seconds and resumes as soon as the database answers again. When a heartbeat times out
the coordinator additionally swaps the connection pool (at most once per lease TTL, log line
`COORDINATOR: database connection pool refreshed`): after a VIP move every pooled connection has the
same dead peer, and without the swap each of them would be tried — and waited for — in turn. Setting
`net.ipv4.tcp_retries2=8` (~100 s) on the container/pod shortens the kernel side as well — this is a
namespaced `net.*` sysctl, so Compose `sysctls:` / Kubernetes `securityContext.sysctls` need no
extra privileges.

## Reference setup

Not shipped as files yet. Outline for Compose (one stack per host):

1. `mariadb:12` with `wsrep_on=ON`, `wsrep_cluster_address=gcomm://host1,host2,host3`,
   ports 4567/4568/4444, `galera_new_cluster` only for the first bootstrap.
2. MaxScale on every core host with `readwritesplit`, `master_accept_reads=true`, monitor
   `galeramon`; `DB_HOST=127.0.0.1` (the local MaxScale).
3. Migrate: `mariadb-dump --single-transaction` from the single MariaDB → import into node 1 →
   nodes 2/3 join via SST.

## Verify

```bash
# preflight "database" is ok (not warn): DB_HOST is the proxy, not a node
kill -9 <galera writer>      # MaxScale elects a new writer; core requests see at most one retry
```

## Known limits

- No shipped Galera/MaxScale reference stack.
- Hot-row writes (`last_used_at` on every API-key use) can cause certification conflicts on
  Galera under heavy load; throttling that write is on the roadmap.
