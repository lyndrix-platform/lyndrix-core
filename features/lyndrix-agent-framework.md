# Feature-Design: `lyndrix-agent` — verschlüsselter Host-Agent + `agent-management`-Plugin

**Status:** 📐 Design (noch kein Code)
**Betroffene Repos (neu):** `lyndrix-agent`, `lyndrix-plugin-agent-management`, `lyndrix-agent-contract`
**Betroffene Repos (Migration):** `lyndrix-plugin-docker-manager`, `lyndrix-plugin-monitoring`, `docker-to-dns`
**Deployment:** Baseline-Service, ausgerollt von der IaC-Pipeline auf jeden Host

---

## 1. Motivation

Heute laufen auf den Hosts als Default mehrere Einzel-Tools, die **unverschlüsselt**
kommunizieren:

| Tool | Zweck | Problem |
|---|---|---|
| `docker-proxy` (socket-proxy) | Docker-Socket über HTTP exponieren | Klartext-HTTP, offener Port im Netz |
| `docker-to-dns` | Docker-Events → DNS-Einträge | eigener Prozess, eigener Kanal |
| `dockermon` | Container-/Host-State liefern | eigener Prozess, eigener Kanal |

Der Schmerz ist im Code sichtbar: **Docker Manager redet direkt per Klartext** an den
Socket-Proxy. In `lyndrix-plugin-docker-manager/app/logic/service.py` baut `_base_url()`
Requests als `http://{ip}:2375/containers/...`; Hosts sind als `{ip, port: 2375,
scheme: "http"}` in Vault hinterlegt. Es gibt sogar Fallback-Logik, wenn der fremde
Proxy `/exec` mit `403` blockt — d.h. wir sind einem Proxy ausgeliefert, dessen
Allowlist wir nicht steuern.

**Kernbeobachtung:** Alle drei Tools brauchen dasselbe — *lokalen Docker-Socket-Zugriff
+ einen sicheren Kanal zum zentralen Core*. Sie unterscheiden sich nur darin, **was**
sie mit dem Socket tun (Kommandos proxen / Events streamen / Stats streamen). Das ist
das Lehrbuch-Muster für **ein Runtime, viele Module** — nicht für drei Images.

Lyndrix-Core bringt genau das Backend mit, das ein Agent-Framework braucht: IAM/API-Keys,
Event-Bus, Vault, SSE-Infrastruktur. Diese Erweiterung nutzt das aus, statt einen
vierten Kommunikationsweg zu erfinden.

---

## 2. Ziele / Nicht-Ziele

**Ziele**
- Ende der Klartext-Kommunikation zu den Hosts — **mTLS als Steady-State-Default**.
- **Ein** Agent-Image pro Host statt N Einzel-Tools; Funktionen zentral zuschaltbar.
- Bestehende Plugins (docker-manager, monitoring) konsumieren Agents **transparent** —
  ohne den Transport zu kennen.
- Kein exponierter Docker-Port mehr im Netz (siehe Transport-Modell).
- Zentrale Sicht: welche Hosts online sind, welche Capabilities, welche Agent-Version.

**Nicht-Ziele (vorerst)**
- Kein generisches Remote-Shell-/Config-Management à la Ansible (die IaC-Pipeline bleibt
  dafür zuständig).
- Kein Ersatz für die IaC-Provisionierung — der Agent wird *von ihr* ausgerollt.
- Keine Multi-Tenancy-Isolierung über einen Core hinaus (ein Core = eine Agent-Flotte).

---

## 3. Grundsatzentscheidung: ein modulares Runtime, keine N Spezial-Images

**Entscheidung:** Ein einziges `lyndrix-agent`-Image mit **zentral schaltbaren
Capability-Modulen**, nicht mehrere zweckgebundene Images.

**Begründung:** N Spezial-Images multiplizieren pro Host den Betrieb — N Deployments,
N Identitäten/Credentials, N Update-Kadenzen, N verschlüsselte Kanäle. Genau der
Schmerz, der heute schon spürbar ist. Ein Image mit Feature-Flags gibt beides: eine
Identität, ein Kanal, ein Rollout — und pro Host aktiviert man zentral nur die
gewünschten Capabilities (`docker-control` / `docker-events` / `docker-stats`).
Zusätzlich spiegelt es Lyndrix-Core selbst (ein modulares Runtime, das Module lädt) —
Konsistenz über die ganze Plattform.

Spezifische Images wären nur gerechtfertigt, wenn Agents fundamental verschiedene
Runtimes/Sprachen bräuchten. Tun sie nicht — es ist immer „Socket + Kanal".

---

## 4. Architektur — drei Komponenten

```
                         ┌─────────────────────────────────────────────┐
                         │                 lyndrix-core                 │
   Consumer-Plugins      │                                             │
   ┌──────────────┐      │   ┌───────────────────────────────────┐    │
   │ docker-mgr   │──────┼──▶│  lyndrix-plugin-agent-management   │    │
   │ monitoring   │◀─Bus─┼───│  • Enrollment / Identität         │    │
   │ dns-consumer │      │   │  • Agent-Registry                 │    │
   └──────────────┘      │   │  • Transport-Endpoint (WS)        │    │
                         │   │  • Service-Fassade + Bus-Bridge   │    │
                         │   └───────────────┬───────────────────┘    │
                         │      Vault (PKI)   │  Event-Bus             │
                         └────────────────────┼───────────────────────┘
                                              │  mTLS (Default: Agent→Core outbound)
                        ┌─────────────────────┼─────────────────────┐
                        │             Host    ▼                     │
                        │   ┌───────────────────────────────────┐   │
                        │   │           lyndrix-agent           │   │
                        │   │  Supervisor + Transport-Client    │   │
                        │   │  Capabilities:                    │   │
                        │   │   • docker-control  ┐             │   │
                        │   │   • docker-events   ├─ /var/run/  │   │
                        │   │   • docker-stats    ┘  docker.sock│   │
                        │   └───────────────────────────────────┘   │
                        └───────────────────────────────────────────┘
```

### 4.1 `lyndrix-agent` (das Image)
Dünner **Supervisor** + **Transport-Client** + **Capability-Module**. Läuft als
Container auf jedem Host mit read-mostly Bind-Mount auf `/var/run/docker.sock`.

Capabilities (zentral pro Host an/aus):
- **`docker-control`** — ersetzt docker-proxy. Führt Docker-API-Calls lokal am Socket
  aus und liefert Ergebnisse über den Kanal zurück. Aktionen laufen gegen eine
  **zentral definierte Allowlist** (nicht mehr die Proxy-Konfig auf dem Host).
- **`docker-events`** — ersetzt die docker-to-dns-Quelle. Streamt Docker-Events nach
  Hause (`agent:docker:event`).
- **`docker-stats`** — zapft dockermon an bzw. ersetzt es. Streamt Container-/Host-Stats
  (`agent:docker:stats`, `agent:host:stats`).
- **`host-metrics`** *(später)* — Node-Level-Metriken (CPU/RAM/Disk), node-exporter-ish.

Der Agent selbst hält **keine** Business-Logik über „Socket + Transport" hinaus. DNS-
Regeln, Monitoring-Auswertung etc. leben zentral.

### 4.2 `lyndrix-plugin-agent-management` (Core-Plugin)
Die Server-Seite. Aufgaben:
- **Enrollment / Identität** — Bootstrap-Token entgegennehmen, Agent registrieren,
  Vault-PKI-Cert-Ausstellung anstoßen (§5).
- **Transport-Endpoint** — der WebSocket-Endpoint, in den Agents reinwählen.
- **Agent-Registry** — persistenter Zustand pro Agent (§6).
- **Service-Fassade + Bus-Bridge** — *der Schlüssel*: andere Plugins nutzen Agents
  über eine stabile Fassade (`agent_service.docker(host_id)...`) und/oder über
  Bus-Events, **ohne** den Transport zu kennen (§7).

### 4.3 `lyndrix-agent-contract` (Shared-Lib)
Gemeinsame **Wire-Protocol-/DTO-Definitionen**, importiert von beiden Seiten (Agent +
Plugin) — analog zu `core.api`. Verhindert Protokoll-Drift zwischen Agent und Core.
Enthält: Frame-Schema, Capability-/Action-Enums, Registry-DTOs, Versionierung.

---

## 5. Transport & Sicherheit — progressives Trust-Modell

Das ist der eigentliche Zweck der Erweiterung. Es gibt **einen Default-Pfad** und
**einen optionalen Beschleuniger**, mit einem gestuften Vertrauensaufbau.

### 5.1 Default-Pfad: Agent → Core (outbound)
Der Agent **wählt raus** und hält eine persistente, authentifizierte Verbindung. Vorteile:
- **Kein inbound Port auf den Hosts** — funktioniert hinter NAT/Firewall, nichts zu
  exponieren.
- Agent initiiert TLS → Verschlüsselung ist der Default, nicht nachträglich draufgeschraubt.
- Zentrale Online/Offline-Sicht über Heartbeat.

### 5.2 Gestufter Vertrauensaufbau
1. **Bootstrap (Tag 0, noch kein Cert):** Der Agent authentifiziert mit einem
   **per-Agent API-Key** (Enrollment-Token) über TLS. Das nutzt die bestehende
   `UserApiKey`/`LYNDRIX_SYSTEM_API_KEY`-Infrastruktur. Reicht, um zu funktionieren
   **und** um Certs zu ziehen.
2. **Enrollment → mTLS (Steady-State-Default):** Der Agent zieht **automatisch** ein
   kurzlebiges Client-Cert aus **Vault PKI** (vom `agent-management`-Plugin vermittelt,
   z. B. via scoped Vault-Token oder einem Sign-CSR-Endpoint). Ab dann verbindet er per
   **mTLS** und **rotiert das Cert selbstständig** vor Ablauf. Das ist der normale Betrieb.
3. **Optionaler Beschleuniger — Core → Agent (inbound), erst wenn mTLS steht:** Sobald
   ein gültiges beidseitiges Cert-Paar existiert, *darf* der Agent zusätzlich einen
   kleinen **mTLS-gesicherten Inbound-Listener** öffnen, den der Core für **schnelle
   Ad-hoc-Abfragen oder schnelle Reconnects** nutzt. Wichtig:
   - Der Port öffnet **nie im Klartext** — er existiert erst, wenn beidseitige Certs da sind.
   - Es ist ein Performance-Boost, **kein** Sicherheits-Downgrade (mTLS beidseitig).
   - Pro Host abschaltbar; **Default bleibt der Agent→Core-Pfad** für alles.

```
Zeitachse eines Agents
──────────────────────
[install] ──API-Key──▶ [enrolled] ──Vault-PKI-Cert──▶ [mTLS steady-state]
                                                            │
                                                            └─(optional)─▶ [+ inbound mTLS für schnelle Calls]
```

### 5.3 Warum das die Verschlüsselungsfrage löst
Der Status quo ist „Core → Host über offenen Klartext-Port". Das neue Modell dreht die
Default-Richtung um (Agent→Core, verschlüsselt) und macht den inbound-Pfad zu einer
**opt-in-Optimierung hinter mTLS**. Es gibt zu keinem Zeitpunkt einen unverschlüsselten
exponierten Socket.

---

## 6. Agent-Registry (Datenmodell, Skizze)

Persistiert im Core (SQLAlchemy, shared `Base`). Felder u. a.:

| Feld | Zweck |
|---|---|
| `agent_id` | stabile ID (UUID) |
| `hostname` / `fqdn` / `address` | Host-Identifikation |
| `enrolled_at` | Zeitpunkt Enrollment |
| `auth_mode` | `api_key` \| `mtls` (Trust-Stufe) |
| `cert_serial` / `cert_expiry` | ausgestelltes Vault-PKI-Cert |
| `capabilities` | aktivierte Module (`docker-control`, …) |
| `status` | `online` \| `offline` \| `degraded` |
| `last_seen` | letzter Heartbeat |
| `agent_version` | für Rollout-/Update-Sicht |
| `inbound_endpoint` | optionaler Core→Agent-Listener (nur wenn mTLS) |

Statuswechsel emittieren Bus-Events (`agent:registered`, `agent:online`,
`agent:offline`), damit die UI/andere Plugins reagieren können.

---

## 7. Integration: wie bestehende Plugins andocken

Der Beweis, dass die Richtung stimmt — die Konsumenten werden **einfacher**:

### 7.1 Wire-Protocol / RPC
Über den Kanal (WebSocket) laufen typisierte Frames. Da docker-manager **synchrone**
Antworten braucht (Container-Liste, Logs), gibt es eine dünne **Correlation-ID-
Request/Reply-Schicht**:

```jsonc
// Request (Core → Agent)
{ "id": "c7f…", "type": "req", "capability": "docker-control",
  "action": "GET", "path": "/containers/json", "payload": null }
// Reply (Agent → Core), gleiche id
{ "id": "c7f…", "type": "rep", "ok": true, "status": 200, "payload": [ … ] }
// Unsolicited Event (Agent → Core), keine id-Korrelation
{ "type": "event", "topic": "docker:event", "payload": { … } }
```

### 7.2 Service-Fassade (für synchrone Consumer)
```python
# in agent-management: from core.api-artiger Fassade
result = await agent_service.docker(host_id).request("GET", "/containers/json")
```
Der Aufrufer weiß nichts von WebSocket/mTLS/Correlation-IDs.

### 7.3 Konkreter Migrationspfad
- **`docker-manager`** — ersetzt `_base_url(host)` + `requests.get(f"{base}/containers/…")`
  durch `agent_service.docker(host_id).request(...)`. Aus „Hosts" (`{ip, port:2375}`)
  werden „Agents". Der 403-`/exec`-Fallback entfällt (Allowlist ist jetzt zentral, nicht
  fremdbestimmt). **Größte Nutznießer-Migration.**
- **`monitoring`** — subscribed `agent:host:stats` / `agent:heartbeat` statt eigenem
  Scrape; dockermon-Daten kommen als Bus-Events rein.
- **`docker-to-dns`** — die DNS-Logik wandert in einen zentralen Consumer (eigenes Plugin
  oder Capability), der auf `agent:docker:event` reagiert. Ein Ort für die Regel statt
  auf jedem Host.

---

## 8. Bus-Topics (Entwurf)

| Topic | Richtung | Payload |
|---|---|---|
| `agent:registered` | Bridge → Bus | agent_id, host, capabilities |
| `agent:online` / `agent:offline` | Bridge → Bus | agent_id, last_seen |
| `agent:heartbeat` | Agent → Bridge | agent_id, ts, load |
| `agent:docker:event` | Agent → Bridge | Docker-Event-Objekt |
| `agent:docker:stats` | Agent → Bridge | container stats |
| `agent:host:stats` | Agent → Bridge | cpu/ram/disk |

---

## 9. Naming & Sprache

- **`iac-agent` vs `lyndrix-agent`:** Das Ding redet mit **Lyndrix-Core** und ist eine
  Lyndrix-Plattform-Komponente — es wird nur *von* der IaC-Pipeline als Baseline
  ausgerollt. Empfehlung: **`lyndrix-agent`** (Runtime) + **`lyndrix-plugin-agent-management`**
  (Plugin) + **`lyndrix-agent-contract`** (Lib). `iac-agent` als Alias/Deployment-Name
  in der Pipeline ist ok.
- **Sprache des Agents:** **lean Python** (asyncio + docker-SDK, schlankes Image) für
  Ökosystem-Konsistenz mit dem Rest der Plattform. **Go** nur, falls der Image-/RAM-
  Footprint auf sehr vielen Hosts kritisch wird (statisches Single-Binary) — dann zahlt
  sich das Shared-Contract-Repo doppelt aus.

---

## 10. Risiken / offene Fragen

- **RPC-Backpressure & Timeouts:** Was passiert, wenn ein Agent langsam/weg ist, während
  docker-manager synchron wartet? → Timeouts + Circuit-Breaker in der Fassade.
- **Vault-PKI-Rollen & Rotation:** genaue Rolle/TTL/Renew-Strategie für Agent-Certs;
  Revocation bei Host-Decommission.
- **Enrollment-Token-Verteilung:** wie kommt der Bootstrap-API-Key sicher auf den Host?
  → über die IaC-Pipeline (SOPS/CI-Variable) beim Ausrollen des Agents.
- **Capability-Allowlist-Format:** wie granular wird `docker-control` (pro Endpoint?
  read/write?) zentral konfiguriert.
- **Agent-Self-Update:** zieht der Agent Updates selbst, oder rollt die IaC-Pipeline neu
  aus? (Vorschlag: Pipeline bleibt Source of Truth, Agent meldet nur seine Version.)
- **Fallback bei Core-Ausfall:** Agent puffert Events lokal (bounded) und replayed nach
  Reconnect?

---

## 11. Roadmap (phasiert)

1. **Contract + Skeleton:** `lyndrix-agent-contract` (Frame-Schema), minimaler Agent
   (Bootstrap per API-Key, Heartbeat), `agent-management` (Enrollment + Registry +
   WS-Endpoint). Ziel: ein Agent taucht online in der Registry auf.
2. **`docker-control` + Fassade:** ein Docker-API-Roundtrip end-to-end; docker-manager
   auf die Fassade migrieren (parallel zum alten Proxy-Pfad, hinter Flag).
3. **mTLS via Vault-PKI:** Auto-Enrollment, Cert-Rotation, Upgrade des Kanals; API-Key
   nur noch Bootstrap.
4. **`docker-stats` + monitoring-Migration**, dann **`docker-events` + DNS-Consumer**.
5. **Optionaler inbound mTLS-Pfad** für schnelle Calls/Reconnects.
6. **IaC-Pipeline:** Agent als Baseline-Service auf allen Hosts ausrollen; alte
   docker-proxy/dockermon/docker-to-dns zurückbauen.

---

*Nächster Schritt laut Absprache: erst dieses Design, dann das finale `lyndrix-core`-
Release. Umsetzung beginnt frühestens mit Phase 1.*
