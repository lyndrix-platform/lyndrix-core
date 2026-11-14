# Changelog

All notable changes to lyndrix-core are documented in this file. Releases before 1.11.1 are
described by the auto-generated notes on
[GitHub Releases](https://github.com/lyndrix-platform/lyndrix-core/releases).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The core is released as the image `ghcr.io/lyndrix-platform/lyndrix-core:<version>`; the version is
`app/version.py` (a push to `main` builds the image and creates the tag `v<version>`).

## [1.11.1] - 2026-11-13
### Fixed
- Database connections against a silently dead peer (DB VIP failover, half-open TCP) no longer block
  for the kernel retransmit timeout (~16 min with `tcp_retries2=15`): the engine sets pymysql
  `read_timeout`/`write_timeout` on every pooled connection (new `DB_SOCKET_TIMEOUT_SECONDS`,
  `0` = auto = `DB_STATEMENT_TIMEOUT_SECONDS` + 30 s, at least 90 s; a value not above the statement
  cap is raised so the server-side cap always fires first). The failing query surfaces as error 2013,
  SQLAlchemy discards the connection and the next call uses a fresh one; `pool_pre_ping` is bounded
  by the same timeout.
- Cluster heartbeat: one hung beat no longer parks the heartbeat task until the socket times out.
  Each beat is abandoned after the lease TTL (`heartbeat_timeout_s`), the next one runs on a fresh
  connection, and the failure is logged with its duration, the time since the last successful beat
  and the TTL after which peers report the instance dead (every one of the first three failures,
  then about once a minute), followed by a recovery line. A successful beat that took longer than
  the interval logs a latency warning. `GET /api/cluster` → `coordinator.heartbeat` carries
  `last_ok_at`, `failures`, `last_duration_s`, `last_error`.
- Lease renewals (`LeaseKeeper.tick`) are bounded by the lease TTL as well, so fencing ("could not be
  renewed for N s — treating it as lost") happens on time instead of after the TCP timeout.

### Operations
- Recommended alongside: `net.ipv4.tcp_retries2=8` on the core/runner containers or pods (namespaced
  `net.*` sysctl, no extra capability) — see `docs/ha/building-blocks/database.md`.
