# Feature design: `lyndrix-agent` — encrypted host agent + `agent-management` plugin

**Status:** 📐 Design (no code yet)
**Affected repos (new):** `lyndrix-agent`, `lyndrix-plugin-agent-management`, `lyndrix-agent-contract`
**Affected repos (migration):** `lyndrix-plugin-docker-manager`, `lyndrix-plugin-monitoring`, `docker-to-dns`
**Deployment:** Baseline service, rolled out by the IaC pipeline to every host

---

## 1. Motivation

Today several individual tools run by default on the hosts and communicate **unencrypted**:

| Tool | Purpose | Problem |
|---|---|---|
| `docker-proxy` (socket proxy) | Expose Docker socket over HTTP | Plaintext HTTP, open port in the network |
| `docker-to-dns` | Docker events → DNS entries | Own process, own channel |
| `dockermon` | Deliver container/host state | Own process, own channel |

The pain is visible in the code: **Docker Manager talks directly in plaintext** to the
socket proxy. In `lyndrix-plugin-docker-manager/app/logic/service.py`, `_base_url()`
builds requests as `http://{ip}:2375/containers/...`; hosts are stored in Vault as `{ip, port: 2375,
scheme: "http"}`. There is even fallback logic when the foreign
proxy blocks `/exec` with `403` — i.e. we are at the mercy of a proxy whose
allowlist we do not control.

**Core observation:** All three tools need the same thing — *local Docker socket access
plus a secure channel to the central core*. They differ only in **what**
they do with the socket (proxy commands / stream events / stream stats). That is the
textbook pattern for **one runtime, many modules** — not for three images.

Lyndrix core already provides exactly the backend an agent framework needs: IAM/API keys,
event bus, Vault, SSE infrastructure. This extension uses that instead of inventing
a fourth communication path.

---

## 2. Goals / non-goals

**Goals**
- End of plaintext communication to the hosts — **mTLS as steady-state default**.
- **One** agent image per host instead of N individual tools; functions centrally switchable.
- Existing plugins (docker-manager, monitoring) consume agents **transparently** —
  without knowing the transport.
- No exposed Docker port in the network anymore (see transport model).
- Central view: which hosts are online, which capabilities, which agent version.

**Non-goals (for now)**
- No generic remote shell/config management à la Ansible (the IaC pipeline remains
  responsible for that).
- No replacement for IaC provisioning — the agent is rolled out *by it*.
- No multi-tenancy isolation beyond one core (one core = one agent fleet).

---

## 3. Fundamental decision: one modular runtime, no N special images

**Decision:** A single `lyndrix-agent` image with **centrally switchable
capability modules**, not several single-purpose images.

**Rationale:** N special images multiply operations per host — N deployments,
N identities/credentials, N update cadences, N encrypted channels. Exactly the
pain that is already noticeable today. One image with feature flags gives both: one
identity, one channel, one rollout — and per host one centrally activates only the
desired capabilities (`docker-control` / `docker-events` / `docker-stats`).
In addition, it mirrors Lyndrix core itself (a modular runtime that loads modules) —
consistency across the whole platform.

Special images would only be justified if agents fundamentally needed different
runtimes/languages. They do not — it is always "socket + channel".

---

## 4. Architecture — three components

```
                         ┌─────────────────────────────────────────────┐
                         │                 lyndrix-core                 │
   Consumer plugins      │                                             │
   ┌──────────────┐      │   ┌───────────────────────────────────┐    │
   │ docker-mgr   │──────┼──▶│  lyndrix-plugin-agent-management   │    │
   │ monitoring   │◀─Bus─┼───│  • Enrollment / identity          │    │
   │ dns-consumer │      │   │  • Agent registry                 │    │
   └──────────────┘      │   │  • Transport endpoint (WS)        │    │
                         │   │  • Service facade + bus bridge    │    │
                         │   └───────────────┬───────────────────┘    │
                         │      Vault (PKI)   │  Event bus             │
                         └────────────────────┼───────────────────────┘
                                              │  mTLS (default: agent→core outbound)
                        ┌─────────────────────┼─────────────────────┐
                        │             Host    ▼                     │
                        │   ┌───────────────────────────────────┐   │
                        │   │           lyndrix-agent           │   │
                        │   │  Supervisor + transport client    │   │
                        │   │  Capabilities:                    │   │
                        │   │   • docker-control  ┐             │   │
                        │   │   • docker-events   ├─ /var/run/  │   │
                        │   │   • docker-stats    ┘  docker.sock│   │
                        │   └───────────────────────────────────┘   │
                        └───────────────────────────────────────────┘
```

### 4.1 `lyndrix-agent` (the image)
Thin **supervisor** + **transport client** + **capability modules**. Runs as a
container on every host with a read-mostly bind mount of `/var/run/docker.sock`.

Capabilities (centrally per host on/off):
- **`docker-control`** — replaces docker-proxy. Executes Docker API calls locally on the socket
  and returns results via the channel. Actions run against a **centrally defined allowlist** (no longer the proxy
  configuration on the host).
- **`docker-events`** — replaces the docker-to-dns source. Streams Docker events home
  (`agent:docker:event`).
- **`docker-stats`** — taps dockermon or replaces it. Streams container/host stats
  (`agent:docker:stats`, `agent:host:stats`).
- **`host-metrics`** *(later)* — node-level metrics (CPU/RAM/disk), node-exporter-like.

The agent itself holds **no** business logic beyond "socket + transport". DNS
rules, monitoring evaluation etc. live centrally.

### 4.2 `lyndrix-plugin-agent-management` (core plugin)
The server side. Tasks:
- **Enrollment / identity** — accept bootstrap token, register agent, trigger
  Vault PKI certificate issuance (§5).
- **Transport endpoint** — the WebSocket endpoint into which agents dial.
- **Agent registry** — persistent state per agent (§6).
- **Service facade + bus bridge** — *the key*: other plugins use agents
  via a stable facade (`agent_service.docker(host_id)...`) and/or via bus
  events, **without** knowing the transport (§7).

### 4.3 `lyndrix-agent-contract` (shared library)
Shared **wire protocol / DTO definitions**, imported by both sides (agent +
plugin) — analogous to `core.api`. Prevents protocol drift between agent and core.
Contains: frame schema, capability/action enums, registry DTOs, versioning.

---

## 5. Transport & security — progressive trust model

This is the actual purpose of the extension. There is **one default path** and
**one optional accelerator**, with a staged trust build-up.

### 5.1 Default path: agent → core (outbound)
The agent **dials out** and maintains a persistent, authenticated connection. Advantages:
- **No inbound port on the hosts** — works behind NAT/firewall, nothing to expose.
- Agent initiates TLS → encryption is the default, not bolted on afterwards.
- Central online/offline view via heartbeat.

### 5.2 Staged trust build-up
1. **Bootstrap (day 0, no cert yet):** The agent authenticates with a
   **per-agent API key** (enrollment token) over TLS. This uses the existing
   `UserApiKey`/`LYNDRIX_SYSTEM_API_KEY` infrastructure. Sufficient to function
   **and** to obtain certificates.
2. **Enrollment → mTLS (steady-state default):** The agent **automatically** obtains a
   short-lived client certificate from **Vault PKI** (arranged by the `agent-management` plugin,
   e.g. via a scoped Vault token or a sign-CSR endpoint). From then on it connects via
   **mTLS** and **rotates the certificate itself** before expiry. This is normal operation.
3. **Optional accelerator — core → agent (inbound), only once mTLS is in place:** As soon as a
   valid mutual certificate pair exists, the agent *may* additionally open a
   small **mTLS-secured inbound listener** that the core uses for **fast ad-hoc queries or fast
   reconnects**. Important:
   - The port **never opens in plaintext** — it only exists once mutual certificates are present.
   - It is a performance boost, **not** a security downgrade (mutual mTLS).
   - Can be disabled per host; **the default remains the agent→core path** for everything.

```
Timeline of an agent
────────────────────
[install] ──API key──▶ [enrolled] ──Vault PKI cert──▶ [mTLS steady-state]
                                                            │
                                                            └─(optional)─▶ [+ inbound mTLS for fast calls]
```

### 5.3 Why this solves the encryption question
The status quo is "core → host over an open plaintext port". The new model reverses the
default direction (agent→core, encrypted) and makes the inbound path an
**opt-in optimization behind mTLS**. At no point is there an unencrypted, exposed socket.

---

## 6. Agent registry (data model, sketch)

Persisted in the core (SQLAlchemy, shared `Base`). Fields include:

| Field | Purpose |
|---|---|
| `agent_id` | stable ID (UUID) |
| `hostname` / `fqdn` / `address` | host identification |
| `enrolled_at` | time of enrollment |
| `auth_mode` | `api_key` \| `mtls` (trust level) |
| `cert_serial` / `cert_expiry` | issued Vault PKI certificate |
| `capabilities` | activated modules (`docker-control`, …) |
| `status` | `online` \| `offline` \| `degraded` |
| `last_seen` | last heartbeat |
| `agent_version` | for rollout/update view |
| `inbound_endpoint` | optional core→agent listener (only with mTLS) |

Status changes emit bus events (`agent:registered`, `agent:online`,
`agent:offline`), so that the UI/other plugins can react.

---

## 7. Integration: how existing plugins connect

The proof that the direction is right — the consumers become **simpler**:

### 7.1 Wire protocol / RPC
Typed frames run over the channel (WebSocket). Since docker-manager needs **synchronous**
replies (container list, logs), there is a thin **correlation-ID
request/reply layer**:

```jsonc
// Request (core → agent)
{ "id": "c7f…", "type": "req", "capability": "docker-control",
  "action": "GET", "path": "/containers/json", "payload": null }
// Reply (agent → core), same id
{ "id": "c7f…", "type": "rep", "ok": true, "status": 200, "payload": [ … ] }
// Unsolicited event (agent → core), no id correlation
{ "type": "event", "topic": "docker:event", "payload": { … } }
```

### 7.2 Service facade (for synchronous consumers)
```python
# in agent-management: from core.api-like facade
result = await agent_service.docker(host_id).request("GET", "/containers/json")
```
The caller knows nothing about WebSocket/mTLS/correlation IDs.

### 7.3 Concrete migration path
- **`docker-manager`** — replaces `_base_url(host)` + `requests.get(f"{base}/containers/…")`
  with `agent_service.docker(host_id).request(...)`. "Hosts" (`{ip, port:2375}`)
  become "agents". The 403 `/exec` fallback is dropped (the allowlist is now central, not externally determined).
  **Largest beneficiary migration.**
- **`monitoring`** — subscribes to `agent:host:stats` / `agent:heartbeat` instead of its own
  scrape; dockermon data arrives as bus events.
- **`docker-to-dns`** — the DNS logic moves into a central consumer (own plugin
  or capability) that reacts to `agent:docker:event`. One place for the rule instead of
  on every host.

---

## 8. Bus topics (draft)

| Topic | Direction | Payload |
|---|---|---|
| `agent:registered` | bridge → bus | agent_id, host, capabilities |
| `agent:online` / `agent:offline` | bridge → bus | agent_id, last_seen |
| `agent:heartbeat` | agent → bridge | agent_id, ts, load |
| `agent:docker:event` | agent → bridge | Docker event object |
| `agent:docker:stats` | agent → bridge | container stats |
| `agent:host:stats` | agent → bridge | cpu/ram/disk |

---

## 9. Naming & language

- **`iac-agent` vs `lyndrix-agent`:** The thing talks to **Lyndrix core** and is a
  Lyndrix platform component — it is only rolled out *by* the IaC pipeline as a baseline.
  Recommendation: **`lyndrix-agent`** (runtime) + **`lyndrix-plugin-agent-management`**
  (plugin) + **`lyndrix-agent-contract`** (library). `iac-agent` as alias/deployment name
  in the pipeline is fine.
- **Language of the agent:** **lean Python** (asyncio + docker SDK, slim image) for
  ecosystem consistency with the rest of the platform. **Go** only if the image/RAM
  footprint becomes critical on very many hosts (static single binary) — then the shared contract repo
  pays off twice.

---

## 10. Risks / open questions

- **RPC backpressure & timeouts:** What happens if an agent is slow/gone while
  docker-manager waits synchronously? → timeouts + circuit breaker in the facade.
- **Vault PKI roles & rotation:** exact role/TTL/renew strategy for agent certificates;
  revocation on host decommissioning.
- **Enrollment token distribution:** how does the bootstrap API key get to the host securely?
  → via the IaC pipeline (SOPS/CI variable) when rolling out the agent.
- **Capability allowlist format:** how granularly is `docker-control` configured centrally (per endpoint?
  read/write?).
- **Agent self-update:** does the agent pull updates itself, or does the IaC pipeline roll it out again?
  (Proposal: pipeline remains source of truth, agent only reports its version.)
- **Fallback on core outage:** does the agent buffer events locally (bounded) and replay them after
  reconnect?

---

## 11. Roadmap (phased)

1. **Contract + skeleton:** `lyndrix-agent-contract` (frame schema), minimal agent
   (bootstrap via API key, heartbeat), `agent-management` (enrollment + registry +
   WS endpoint). Goal: one agent appears online in the registry.
2. **`docker-control` + facade:** one Docker API round trip end-to-end; migrate docker-manager
   to the facade (in parallel with the old proxy path, behind a flag).
3. **mTLS via Vault PKI:** auto-enrollment, certificate rotation, upgrade of the channel; API key
   only as bootstrap.
4. **`docker-stats` + monitoring migration**, then **`docker-events` + DNS consumer**.
5. **Optional inbound mTLS path** for fast calls/reconnects.
6. **IaC pipeline:** roll out the agent as baseline service on all hosts; dismantle the old
   docker-proxy/dockermon/docker-to-dns.

---

*Next step as agreed: first this design, then the final `lyndrix-core`
release. Implementation begins at the earliest with phase 1.*
