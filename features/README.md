# `features/` — planned extensions & new plugins

This folder collects **design documents for planned extensions** of Lyndrix —
new plugins, larger core rework, platform-wide concepts. Every idea gets
a `.md` file **before** code is written: motivation, architecture decision,
migration path, open questions. This keeps the direction open for discussion while it is still cheap to change.

Convention (as in the rest of the repo): **prose in German, code/identifiers/APIs in English.** A doc is "done" when its points are implemented and released —
then the lasting part moves into the regular `documentation.md` / the plugin repo,
and the feature doc may be removed (or marked as "shipped").

## Index

| Doc | Topic | Status |
|---|---|---|
| [`step-ca-ui-plugin.md`](./step-ca-ui-plugin.md) | PKI plugin as a web UI for step-ca (certificates, expiry monitoring, revoke, entity registry) — none of the OSS UIs fit | 💡 Idea |
| [`lyndrix-agent-framework.md`](./lyndrix-agent-framework.md) | Encrypted host agent (`lyndrix-agent`) + `agent-management` plugin — replaces plaintext docker-proxy/dockermon/docker-to-dns | 📐 Design |
| [`rbac-security-k8s-audit.md`](./rbac-security-k8s-audit.md) | RBAC expansion (plugin roles + assignment API), security findings from the multi-agent audit (incl. critical superadmin escalation) & Kubernetes multi-pod readiness | 📐 Design / Audit |
| [`cluster-plugin-ops-and-ux.md`](./cluster-plugin-ops-and-ux.md) | Cluster-wide plugin operations (one notification per upgrade, progress per instance, DB notification store), "Update all", gear icon for plugin settings, external-services configuration in React + optional grid, Users page role-oriented with implied rights | 🚧 started 2026-10-16 |
| [`high-availability.md`](./high-availability.md) | High availability: N backends / frontends / DB nodes / Vault nodes — state inventory of core + all plugins, target architecture (DB-based coordination layer, leader lease, `placement`), Galera single writer, Vault Raft, phase plan, deployment profiles Compose×3 vs. k3s | 🚧 Phase 0+1 implemented (Core 1.4.0 / API 1.7) |
| [`high-availability-implementation.md`](./high-availability-implementation.md) | HA implementation modular for Docker **and** Kubernetes: tier model, configuration surface (`LYNDRIX_CLUSTER_MODE` …), `cluster`/`storage` component, migrate CLI, drain, LB contract + options for the separate Docker LB, repo `lyndrix-deploy`, new doc tree `docs/ha/`, milestones M0–M6 | 🚧 M0+M1 implemented, operator docs in `docs/ha/`; M2–M6 open |
