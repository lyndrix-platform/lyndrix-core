# Wartungsfenster, geplante Deployments und Incident-Regeln

Stand: 2026-10-17, Planungsdokument (Phase 0). Autor: Lyndrix-Session mit Marvin.
Status: **P0 abgeschlossen.** Die sechs Analysen und die Teilpläne liegen in
`features/maintenance-windows/`. **`00-ergebnis-p0.md` ersetzt die Abschnitte 4, 6 und 7 dieses
Dokuments, wo es ihnen widerspricht** (Typ-Vokabular ohne `vm`, Entscheidung im Job statt beim
Enqueue, lokale Bus-Emission, Override-Semantik, ITSM-Mapping). Nächster Schritt: Review der
Teilpläne mit Marvin (offene Entscheidungen A–H im Ergebnis, Abschnitt 5), dann P1.

## 1. Ziel

Jede überwachbare Komponente – Hardware (PVE-Nodes), VMs, Container, Services – kann
geplante **Wartungsfenster** haben. Innerhalb eines Fensters alarmiert das Monitoring nicht
(Zustand „Wartung“ statt „down“, keine Benachrichtigungen, keine Incidents).

Deployments, die aus dem Repository kommen (Renovate → GitLab-Webhook → IaC Orchestrator),
werden **in das nächste Wartungsfenster** des betroffenen Services/Hosts geplant. Der Operator
kann das übersteuern („jetzt ausführen“); dieser **Override** wird als temporäres Wartungsfenster
an das Monitoring weitergereicht, damit das Deployment selbst keinen Alarm auslöst.

Hat ein Service kein eigenes Fenster, gilt ein **Default-Wartungsfenster**, das im iac-controller
aktivierbar ist. Fenster für Hardware, VMs und Container werden im iac-controller konfiguriert,
Fenster für Services in der `service.yml` des Services (SSoT).

Zusätzlich gibt es pro Service **Incident-Regeln**: Ist Service X außerhalb eines (geplanten
oder übersteuerten) Fensters länger als Y Minuten offline (Y aus `service.yml`), entsteht ein
Incident im ITSM, verknüpft mit dem Host, auf dem der Service läuft, und mit dem Service selbst.

## 2. Leitprinzipien

1. **Jedes Plugin implementiert seine Wartungsfähigkeit selbst.** Kein Plugin *braucht* ein anderes.
   Wer nur den IaC Orchestrator installiert hat, bekommt geplante Deployments und Overrides; die
   Events an das Monitoring verhallen dann folgenlos (`ctx.emit` ist fire-and-forget). Wer nur das
   Monitoring hat, pflegt Fenster direkt dort. Zusammen ergibt sich die Erweiterung.
2. **Der Core bleibt plugin-agnostisch.** Er liefert nur eine neutrale Bibliothek (Fenster-Modell,
   Zeitauswertung) und den dokumentierten Bus-Vertrag (Topics, Payloads). Keine Persistenz, keine
   Plugin-Kenntnis im Core.
3. **Single Source of Truth bleibt der SSoT.** Fenster und Incident-Regeln stehen im
   iac-controller (Hosts, Stages) und in der `service.yml` (Services). Lyndrix rendert, zeigt an
   und wertet aus – es ist nicht die zweite Wahrheit. Was in Lyndrix konfiguriert wird (Overrides,
   manuelle Fenster im Monitoring, Default-Schalter) ist Laufzeitzustand, kein Duplikat des SSoT.
4. **CI-Identität über die Entity Registry** (`ci_id`, bestehender Vertrag): Hosts, VMs, Services
   werden in allen Events über ihre `ci_id` adressiert, nie über IP oder Namen allein.
5. **Degradieren statt brechen:** fehlt ein Konsument, fehlt ein `ci_id`, fehlt eine Regel → das
   jeweilige Plugin verhält sich wie heute.

## 3. Begriffe

| Begriff | Bedeutung |
|---|---|
| Wartungsfenster (Schedule) | Wiederkehrend (z. B. jeden Sonntag 02:00–04:00 Europe/Berlin) oder einmalig (Start/Ende). |
| Default-Fenster | Fenster, das für Komponenten ohne eigenes Fenster gilt; pro Site/Stage definierbar, im iac-controller aktivierbar. |
| Vererbung | Service → Host → Stage/Site-Default → aus. Ein Service ohne eigenes Fenster erbt das seines Hosts, dann das Stage-Default. |
| Override | Temporäres Fenster, das ein Operator oder ein Deployment-Job erzeugt (Start = jetzt, Ende = Jobende + Puffer). Gilt für die betroffenen `ci_id`s. |
| Geplantes Deployment | Ein Orchestrator-Job im Zustand `SCHEDULED` mit `not_before` = Beginn des nächsten Fensters. |
| Incident-Regel | Pro Service: „offline ≥ Y Minuten außerhalb eines Fensters ⇒ Incident (Severity, Auto-Resolve)“. |

## 4. Konfigurationsquellen (SSoT)

### 4.1 `service.yml` (aac-Service, Template Engine validiert)

```yaml
maintenance:
  windows:                       # optional; leer = erbt Host/Stage-Default
    - weekly: { days: [sun], start: "02:00", end: "04:00", tz: Europe/Berlin }     # einfaches Format
    - once:   { start: "2026-11-02T22:00+01:00", end: "2026-11-03T02:00+01:00", note: "Storage-Umzug" }
    - cron:   { expr: "0 2 1 * *", duration_minutes: 180, tz: Europe/Berlin }       # erweitert (wie Semaphore: Cron + Dauer)
  deploy_policy: window          # window | immediate; Default: prod → window, dev/test → immediate
  override_buffer_minutes: 15    # Override-Fenster = Jobdauer + Puffer
incidents:                       # überschreibt die globalen Typ-Defaults (controller) für diesen Service
  enabled: true
  offline_minutes: 10            # Y; 0 = keine Incident-Regel
  severity: high                 # ITSM-Severity
  auto_resolve: true             # Incident schließen, wenn der Service wieder online ist
```

### 4.2 iac-controller (Hosts, Hardware, VMs, Stages)

```yaml
# hosts.yml / Hardware / VM-Definitionen
maintenance:
  windows: [...]                 # wie oben
  inherit: true                  # Services auf diesem Host erben, wenn sie nichts eigenes haben
# stages/<stage>.yml (oder Site)
maintenance_default:
  enabled: true                  # der „Default-Fenster“-Schalter (auch aus der Orchestrator-UI schaltbar → Git-Commit)
  windows:
    - weekly: { days: [sun], start: "02:00", end: "04:00", tz: Europe/Berlin }
incident_defaults:               # globale Regeln je CI-Typ (Typen = Entity-Registry-Kinds)
  service: { enabled: true,  offline_minutes: 10, severity: high,   auto_resolve: true }
  host:    { enabled: true,  offline_minutes: 5,  severity: critical, auto_resolve: true }
  vm:      { enabled: true,  offline_minutes: 5,  severity: high,   auto_resolve: true }
  container: { enabled: false }
```

Auflösung der Incident-Regel: Service-/Host-Eintrag im SSoT → `incident_defaults[<typ>]` der Stage →
aus. Zusätzlich hat das Monitoring einen **Laufzeit-Notschalter je Typ** (Plugin-Setting), der die
Erzeugung von Outage-Events ohne Repo-Änderung stoppt – kein zweites Regelwerk, nur ein Aus-Knopf.

Das Rendering (Template Engine / `iac_core` im Orchestrator) löst die Vererbung auf und liefert pro
Host/Service die **effektiven** Fenster und Regeln in die Inventory-Variablen; der Orchestrator
veröffentlicht sie (Abschnitt 6).

Entschieden: Der Default-Schalter wird aus der Orchestrator-UI bedient und per Git-Commit in den
iac-controller zurückgeschrieben (eine Wahrheit, bequem). Der Orchestrator braucht dafür Schreibrechte
auf das controller-Repo (bestehender Git-Pfad des Plugins, Commit-Autor „Lyndrix IaC Orchestrator“).

## 5. Core: Bibliothek und Vertrag (API 1.13)

`core.api.maintenance` – reine Funktionen, keine Persistenz:

- `Schedule` (weekly | once | cron+duration), `MaintenanceWindow` (ci_id, source, schedule, note, override:bool);
  Cron-Auswertung über `croniter` (oder eigene Minimal-Implementierung) mit Zeitzone
- `parse_windows(raw: list[dict]) -> list[Schedule]` (validiert das YAML-Schema aus 4.x)
- `active_until(schedules, now) -> datetime | None` (laufendes Fenster), `next_start(schedules, after)`
- Topic-Konstanten und Payload-Schemata des Bus-Vertrags (Abschnitt 6), damit alle Plugins dieselben
  Namen verwenden; Doku in `docs/plugins.md` („Maintenance (API 1.13)“).

Keine neuen Core-Endpunkte außer ggf. `GET /api/maintenance/contract` (reine Doku) – bewusst
minimal, der Core kennt keine Fenster.

## 6. Bus-Vertrag (alles optional, fire-and-forget)

| Topic | Emitter | Payload | Konsumenten |
|---|---|---|---|
| `maintenance:windows_published` | Orchestrator (nach Render/Sync), Monitoring (manuelle Fenster) | `{source, items:[{ci_id, kind: host|vm|service, windows:[Schedule], inherited_from?}], replaces: true}` | Monitoring, Registry, Server Manager, ITSM |
| `maintenance:override_started` | Orchestrator (Deploy jetzt), Monitoring (manuell), ITSM (Change) | `{override_id, ci_ids[], until, reason, source}` | Monitoring, Registry |
| `maintenance:override_ended` | dito | `{override_id}` | dito |
| `monitoring:outage_persisted` | Monitoring | `{ci_id, host_ci_id?, service?, since, duration_s, rule:{offline_minutes, severity, auto_resolve}}` | ITSM |
| `monitoring:outage_recovered` | Monitoring | `{ci_id, since, recovered_at}` | ITSM (Auto-Resolve) |

Regeln: `ci_id` Pflicht (Emitter löst vorher über die Registry auf, sonst kein Event); Konsumenten
ignorieren unbekannte Felder; jeder Konsument hält seinen eigenen Zustand (Tabelle) und kommt ohne
den Emitter aus. Topics werden in den Manifesten unter `permissions.emit/subscribe` deklariert.

## 7. Arbeit pro Plugin

### 7.1 IaC Orchestrator (`lyndrix-plugin-iac-orchestrator`)
- SSoT-Parsing: `maintenance`/`incidents` aus `service.yml` und controller-Hosts/Stages lesen,
  Vererbung auflösen, pro CI veröffentlichen (`maintenance:windows_published`, nach jedem Sync).
- Job-Queue: neue Zustände `SCHEDULED` (+ `not_before`, `window_ci_ids`); Webhook-Deploys mit
  `deploy_policy: window` werden nicht sofort `QUEUED`, sondern `SCHEDULED` auf den nächsten
  Fensterbeginn; der Worker nimmt sie fällig auf. Mehrere Deploys desselben Services im selben
  Fenster werden zusammengefasst (letzter Stand gewinnt).
- UI: Liste geplanter Deployments mit Fenster, Button „Jetzt ausführen“ → Override: Job sofort
  `QUEUED`, `maintenance:override_started` für Host+Service-CIs (Ende = Jobende + Puffer),
  `override_ended` beim Abschluss.
- Default-Fenster-Schalter: Anzeige des effektiven Default je Stage; Aktivierung je nach Frage 1.
- Guardrails: ohne Monitoring keine Änderung im Verhalten außer dem Planen; ohne `ci_id` kein Event.

### 7.2 Monitoring (`lyndrix-plugin-monitoring`)
- Eigene Tabelle `monitoring_maintenance` (ci_id/monitor_id, schedule JSON, source, note) und
  `monitoring_overrides` (override_id, ci_ids, until, reason); Konsument der Bus-Events, zusätzlich
  **manuelle Fenster in der eigenen UI** (pro Monitor/Host, einmalig oder wiederkehrend).
- Auswertung beim Probe-Ergebnis: ist für den Monitor (über `ci_id`) ein Fenster/Override aktiv →
  Zustand `maintenance` statt `down`, keine Notification (Discord/Notification-Router), kein
  Transition-Event als Alarm; Dashboard zeigt „Wartung bis hh:mm“.
- Incident-Regel: pro Monitor die Regel aus dem Bus (`rule`) oder lokal konfiguriert; läuft ein
  Monitor außerhalb eines Fensters ≥ `offline_minutes` → einmal `monitoring:outage_persisted`;
  bei Rückkehr `monitoring:outage_recovered`. Zustand in einer kleinen Tabelle, cluster-sicher
  (nur die Prober-Lease-Instanz wertet aus).

### 7.3 ITSM (`lyndrix-plugin-itsm`)
- Konsument: `monitoring:outage_persisted` → Incident anlegen (Dedupe: offener Incident je `ci_id`),
  Severity aus der Regel, CI-Links auf Service- und Host-CI, Beschreibung mit Dauer/Monitor.
- `monitoring:outage_recovered` → Auto-Resolve (wenn Regel es erlaubt), sonst Kommentar.
- Eigene Wartungskenntnis: ein geplanter Change kann ein Fenster als Override veröffentlichen
  (`maintenance:override_started`) – zweite Stufe, nicht Teil des ersten Wurfs.

### 7.4 Entity Registry, Server Manager, Docker-Manager, External Services
- Registry: Attribut `maintenance` am CI (aktuelles/nächstes Fenster, Quelle) aus den Bus-Events –
  read model, damit jede UI „in Wartung“ anzeigen kann (`GET /entities/{id}` erweitert).
- Server Manager: Anzeige des effektiven Fensters und laufender Overrides am Host.
- Docker-Manager / External Services: Anzeige-Badge, optional eigene manuelle Overrides (später).

### 7.5 IaC-Workspace (Zusammenarbeit mit der IaC-Session)
- Template Engine: Schema für `maintenance`/`incidents` in `service.yml`, Validierung, Rendering
  der effektiven Werte in die Inventory-Variablen.
- iac-controller: Schema für Hosts/Stages (`maintenance`, `maintenance_default`), Dokumentation.

### 7.6 Shell (lyndrix-ui)
- Nichts Zentrales nötig; Plugins zeigen ihre eigenen Badges. Optional später: Wartungs-Badge in der
  Plugin-Liste aus dem Registry-Read-Model.

## 8. Phasen

| Phase | Inhalt | Wer |
|---|---|---|
| P0 | Analyse-Sweeps pro Plugin (günstige Subagenten): aktuelle Datenmodelle, Events, UI-Stellen, Teststand, Lücken gegenüber Abschnitt 7 | Sonnet/Haiku-Agenten, Briefings in Abschnitt 10 |
| P1 | Core-Bibliothek + Vertrag + Doku (API 1.13), Tests der Zeitlogik (Zeitzonen, Monatswechsel, Sommerzeit) | Fable |
| P2 | Orchestrator: Parsing, `SCHEDULED`, Override, UI | Fable-Subagent mit Teilplan |
| P3 | Monitoring: Tabellen, Suppression, manuelle Fenster, Outage-Regel | Fable-Subagent mit Teilplan |
| P4 | ITSM: Incident aus Outage, Auto-Resolve, CI-Links | Fable-Subagent mit Teilplan |
| P5 | Registry/Server Manager Read-Model und Anzeige | Fable-Subagent mit Teilplan |
| P6 | IaC: Template-Engine- und controller-Schema, Doku; Übergabe an die IaC-Session | IaC-Session |
| P7 | Dev-Bench-Durchstich: Renovate-Deploy → SCHEDULED → Override → Monitoring „Wartung“ → Incident-Regel außerhalb Fenster → ITSM | Fable |

Jede Phase endet mit Tests (reine Funktionen + Plugin-Suiten im Dev-Core) und einem Release-Tag;
Reihenfolge der Rollouts wie gewohnt (Core → Plugins → k3s-Roll durch Marvin).

## 9. Entscheidungen (Marvin, 2026-10-17)

1. **Default-Fenster-Schalter:** Definition im iac-controller, Ein/Aus auch aus der Orchestrator-UI
   mit Rückschreiben per Git-Commit (eine Wahrheit).
2. **Deploy-Standardverhalten:** `prod` wartet auf das nächste Fenster, `dev`/`test` deployen sofort;
   pro Service per `deploy_policy` übersteuerbar.
3. **Fensterformat:** beides – einfaches Schema (`weekly`, `once`) als Standard, erweitert
   `cron` + `duration_minutes` (wie Semaphore).
4. **Incidents:** Auto-Resolve bei Rückkehr, Regeln für Services **und** Hosts/VMs; **globale
   Regeln je CI-Typ** (Typen aus der Entity Registry) als Default, pro Service in der `service.yml`
   bzw. pro Host im controller übersteuerbar; Laufzeit-Notschalter je Typ im Monitoring.
5. Annahmen ohne Rückfrage: mehrere Deploys desselben Services im selben Fenster → letzter Stand
   gewinnt; Override-Puffer 15 Minuten, maximale Override-Dauer 4 h (beides als Setting).

**Nachtrag 2026-10-18 (Antworten auf A–H aus `maintenance-windows/00-ergebnis-p0.md` §5):**
Punkt 1 ist präzisiert: der Orchestrator bleibt Ausführung und zeigt den Default-Schalter nur
an; das Rückschreiben per Git-Commit kommt in ein eigenes Vorhaben im SSoT-Manager-Plugin
(C). Typen sind alle Entity-Registry-Kinds, offen für künftige wie `network` (A). Findet sich
im Horizont kein Fenster, deployt der Orchestrator sofort mit Warnung und das ITSM legt einen
Task „Wartungsfenster definieren" an (H, Topic `maintenance:window_missing`). B, D, E, F, G
mit den vorgeschlagenen Defaults.

## 11. Ergebnis P0 (2026-10-17)

| Datei | Inhalt |
|---|---|
| `maintenance-windows/00-ergebnis-p0.md` | Befunde, korrigierter Vertrag (Schema, Payloads, Transportregel), Architekturentscheidungen, offene Entscheidungen A–H, Phasen/Versionen, P7-Durchstich |
| `maintenance-windows/analysis-{iac-orchestrator,monitoring,itsm,entity-registry,server-manager,template-engine-controller}.md` | Analyse-Sweeps (Sonnet), file:line-genau |
| `maintenance-windows/plan-core.md` | P1: `core.api.maintenance`, API 1.13, Core 1.11.0, Shell 1.8.0 |
| `maintenance-windows/plan-iac-orchestrator.md` | P2: Resolver, Service-CIs, `SCHEDULED`/Park, Overrides, Default-Schalter, 1.6.0 |
| `maintenance-windows/plan-monitoring.md` | P3: `ci_id`, `MAINTENANCE`, Fenster/Overrides, Outage-Regel, 1.7.0 |
| `maintenance-windows/plan-itsm.md` | P4: Incidents aus Outage-Events, Auto-Resolve, 0.3.0 |
| `maintenance-windows/plan-entity-registry.md` | P5a: Read-Model, Scheme `iac.service`, 0.3.0 |
| `maintenance-windows/plan-server-manager.md` | P5b: Badge/Filter, 1.4.0 |
| `maintenance-windows/plan-iac-workspace.md` | P6: Linter, Controller-Doku/Beispiele, CI-Statusvertrag (Übergabe IaC-Session) |

## 10. Briefings für die Analyse-Sweeps (P0)

Jeder Sweep liefert eine Markdown-Datei `features/maintenance-windows/analysis-<plugin>.md` mit:
Datenmodell (Tabellen/Spalten, `ci_id`-Nutzung), relevante Events (emit/subscribe), API-Routen,
UI-Komponenten mit Datei:Zeile, Teststand/-recipe, Cluster-Besonderheiten (Lease, Shard), und
eine Liste konkreter Einhängepunkte für Abschnitt 7 – ohne Code zu ändern.

- Orchestrator: Webhook-Pfad (GitLab/Renovate) bis zum Job, Queue-Zustände, Worker, wo `service.yml`
  und controller-Daten gelesen werden, `iac:inventory_updated`-Payload, UI-Jobliste.
- Monitoring: Monitor-Modell, Probe-Loop, Transition/Notification-Pfad, `ci_id` am Monitor,
  Inventory-Sync aus dem Orchestrator, Dashboard-Komponenten, Prober-Lease.
- ITSM: Incident-Modell, Erzeugung per API/Bus, CI-Links, Dedupe-Möglichkeiten, Katalog-YAML.
- Entity Registry: Entity-Attribute, Erweiterbarkeit, `lookup`/`identify`-Verträge.
- Server Manager: Host-Modell, Anzeigeorte.
- Template Engine / iac-controller: `service.yml`-Schema-Validierung, Render-Pfad, Stage-Overrides.
