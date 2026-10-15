# Building block: central log

## Contract

- Every instance keeps its own in-memory ring buffer (`GET /api/logs`, the fast path for *this*
  process) **and** ships its log lines to the shared table `cluster_logs`.
- The UI can open the log of **any** instance, or all instances merged, from whichever instance the
  load balancer picked (`GET /api/logs?instance=<id|all>`); the lines survive a pod restart.
- The leader prunes the table by age and by a per-instance cap. Shipping is best effort — a
  disconnected database drops lines (counted), it never blocks logging or the event loop.

## How it works

| Piece | Where |
|---|---|
| `ClusterLogHandler` on the root logger buffers formatted records (≥ `LYNDRIX_LOG_DB_LEVEL`, minus `LYNDRIX_LOG_DB_EXCLUDE`) | `core/logger.py`, `cluster/logic/log_store.py` |
| `LogStore` flushes the buffer every 2 s into `cluster_logs` (`instance_id, created_at, source, level, message`) once the coordinator is online — single mode included | `cluster_service._start_once()` |
| Prune every ~3 min on the leader: rows older than `LYNDRIX_LOG_DB_RETENTION_HOURS`, more than `LYNDRIX_LOG_DB_MAX_PER_INSTANCE` per instance | `LogStore.prune()` |
| API: `GET /api/logs` (local ring), `GET /api/logs?instance=<id|all>&source=&level=&limit=&since_id=` (shared store), `/api/logs/sources?instance=`, `/api/logs/instances` | `core/api/logs_api.py` |
| UI: the log modal (Plugins page per plugin, Settings → Cluster per instance) has an instance selector — *This instance (live)*, *All instances*, or one instance; merged view prefixes each line with `[instance]` | `lyndrix-ui/src/components/LogModal.tsx` |

Lines carry `id`, `instance`, `ts`, `source`, `level`, `message`; pass the last `id` as `since_id`
to tail incrementally.

## Lyndrix settings

| Variable | Default | Meaning |
|---|---|---|
| `LYNDRIX_LOG_DB_ENABLED` | `true` | ship lines to `cluster_logs` |
| `LYNDRIX_LOG_DB_LEVEL` | `INFO` | minimum level shipped |
| `LYNDRIX_LOG_DB_EXCLUDE` | `uvicorn.access` | comma list of logger names; trailing `*` = prefix |
| `LYNDRIX_LOG_DB_RETENTION_HOURS` | `24` | age retention |
| `LYNDRIX_LOG_DB_MAX_PER_INSTANCE` | `5000` | per-instance cap |

## Sizing

An instance logs a few INFO lines per second; with the default cap the table stays at
`instances × 5000` rows (a few MB). Dashboards polling the modal issue one indexed range query per
tick. Debug logging stays local (file + ring buffer) unless `LYNDRIX_LOG_DB_LEVEL=DEBUG`.

## Verify

Open Settings → Cluster → *Logs* on an instance that is **not** the one answering (compare
`answered_by` in the API response with the selected instance), or run
`GET /api/logs/instances` — every live instance must appear with a recent `last_ts`.

## Known limits

- No full-text search; `source` and `level` filters only (grep the host file for forensics).
- The handler buffer holds 5000 lines; a database outage longer than that loses the oldest lines
  for the shared store (the file and the ring buffer are unaffected).
- Logs of plugins' subprocesses (Ansible runner containers) are **not** here — they are the
  orchestrator's job logs (`iac_orchestrator_job_logs`).
