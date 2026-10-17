# P0-Ergebnis: Befunde, korrigierter Vertrag, Entscheidungen

Stand 2026-10-17. Synthese der sechs Analyse-Sweeps (`analysis-*.md` in diesem Ordner) durch die
Lyndrix-Session (Fable). Dieses Dokument ersetzt die Abschnitte 4, 6 und 7 des Hauptplans
`../maintenance-windows.md`, wo es ihnen widerspricht. Die Teilpläne `plan-*.md` bauen darauf auf.

## 1. Befunde, die den Plan ändern

| # | Befund | Quelle | Konsequenz |
|---|---|---|---|
| 1 | Die Entity Registry kennt die Kinds `host`, `hypervisor`, `service`, `container`, `network`, `itsm_record`, `generic`. **Es gibt kein `vm`.** VMs und LXCs sind `host`, physische Nodes `hypervisor`. | registry §2.2 | Typ-Vokabular des Features = `hypervisor \| host \| service \| container`. `incident_defaults` wird mit diesen Schlüsseln geführt. |
| 2 | **Niemand legt `service`-Entities an**, und die Relation `runs_on` wird von keinem Plugin gepflegt. Der Orchestrator identifiziert nur Hosts und Hypervisoren. | registry §2.2, §2.5; orchestrator §2 | Der Orchestrator identifiziert künftig Services (`entity_type: service`, neues Scheme `iac.service = <host>/<service.name>`) und setzt `runs_on` Service → Host. Events tragen zusätzlich `host_ci_id`, damit Konsumenten nicht auf die Relation angewiesen sind. |
| 3 | **Das Monitoring hat kein `ci_id`** am Monitor, nutzt keine Registry und bekommt sein Inventar per HTTP-POST vom Orchestrator (`/inventory-sync`), nicht über den Bus. | monitoring §1, §4 | Der Orchestrator reichert den Inventory-Sync-Payload um `ci_id` (Hosts und Services) und `host_ci_id` (Services) an; das Monitoring speichert `ci_id` am Monitor. Reihenfolge in `NativeGenerateStage`: erst `_attach_ci_ids`, dann Monitoring-Sync. |
| 4 | **Das Monitoring hat keinen Notification-Pfad.** `monitoring:state_changed` hat keinen Konsumenten. | monitoring §1, §10.2 | „Keine Alarme im Fenster" heißt konkret: Zustand `maintenance` statt `down`, keine Outage-Events, und jeder spätere Alarmpfad muss `in_maintenance` prüfen. Kein Discord-Hook zu bauen. |
| 5 | **Der Orchestrator liest `service.yml` nicht**; `iac_core` liest nur den Controller. Die Template Engine sieht `service.yml` nur zur Deploy-Zeit auf dem Runner. | orchestrator §8.1, §9.1; TE §5 | Die Auflösung der effektiven Fenster passiert im Orchestrator auf dem Runner (Variante A): Controller-Baum über `iac_core.loader` plus `services_dir/*/service.yml`. Die Template Engine validiert nur (Linter) und reicht durch. |
| 6 | Die Entscheidung „sofort oder geplant" fällt heute beim Enqueue auf einer beliebigen Control-Plane-Instanz, die **keine Git-Working-Trees** hat. | orchestrator §8.2.5 | **Entscheidung im Job** („Park"): der Runner claimt den Job, klont das Service-Repo (bestehende erste Stage), wertet die Policy aus und parkt den Job als `SCHEDULED` mit `not_before`. Immer frisch, kein Snapshot für die Entscheidung nötig. |
| 7 | Die Service-CI (`prod-trigger-orchestrator`) pollt bis 1800 s auf `success`/`failed`; `QUEUED` und ein künftiges `SCHEDULED` werden zu `unknown` → der CI-Job wird rot. | TE §8; orchestrator §3.6 | Status-Endpunkt liefert `scheduled` + `not_before` nur, wenn der Client `?accept=scheduled` sendet (neues CI-Template, P6). Alte Templates bekommen weiter `success` mit `job_status: SCHEDULED` (Kompatibilitätsmodus). |
| 8 | Doppelte Deploys: heute **gewinnt der erste** (`DuplicateJob`), nicht der letzte Stand. | orchestrator §7 | Nur für `SCHEDULED`-Jobs wird zusammengefasst (Payload und `start_time` aktualisieren). Für `QUEUED`/`RUNNING` bleibt das heutige Verhalten. |
| 9 | Der Orchestrator hat **keinen Schreibpfad ins Controller-Repo**; die Rolle `iac_controller` ist read-only konfiguriert; der Commit-Autor im Core ist fest „Lyndrix IaC". `stage_vars.yml` enthält einen TF-Backend-Block mit einem Credential-Keynamen. | orchestrator §8.4 | Default-Schalter als eigener Pipeline-Typ auf dem Runner mit **zielgenauer Textänderung** (nur die Zeile `enabled:` im Block `maintenance_default`), kein YAML-Roundtrip. Push-Recht für den Controller-Credential ist Marvins Entscheidung; ohne Recht bleibt der Schalter in der UI deaktiviert mit Hinweis. |
| 10 | Das Override-Ende ist vorab unbekannt; es gibt kein Bus-Completion-Event für Jobs. | orchestrator §9.12 | `override_started` mit `until = now + max_override_minutes` (Default 240), `override_ended` in `run_pipeline finally`, Orphan-Sweep und Abort. Konsumenten lassen das Override nach `ended` noch `linger_minutes` (Default 15) nachlaufen. |
| 11 | ITSM-Incidents haben **keine Severity**, sondern Priorität P1–P4 aus `impact × urgency`. „Offen" ist kein Katalogfilter (`closed_at IS NULL` schließt `resolved` ein). `on_hold → resolved` ist verboten. | itsm §3, §9.3–9.5 | Mapping-Tabelle `critical→1/1`, `high→1/2`, `medium→2/2`, `low→3/2`. Dedupe über State-Kind `open`. Auto-Resolve aus `on_hold` fällt auf einen Kommentar zurück. Neuer Katalog-Code `auto_recovered`. |
| 12 | Jede Instanz führt alle Bus-Handler aus; kein Konsument hat heute einen Single-Writer-Schutz. | itsm §8, registry §8, server-manager §7 | Transportregel in Abschnitt 3: lokale Emission, idempotente Handler, DB als Zustand. ITSM nutzt `UNIQUE(source, external_id)` mit deterministischem `external_id = outage_id`. |
| 13 | Controller-Layout ist `environments/sites/<site>/stages/<stage>/{stage,stage_vars,hosts}.yml`; nur Keys **unter `stage_vars:`** werden Hostvars; Hardware-Hosts bekommen keine stage_vars. Listen werden im Loader vereinigt, in der Template Engine ersetzt. | TE §4, §6, §9 | `maintenance_default` und `incident_defaults` liegen unter `stage_vars:` (Stage) bzw. `site_vars:` (Site, gilt für Hardware). Fensterlisten werden pro Ebene **nur auf einer Ebene** definiert (Vererbung ersetzt, nie gemischt). |
| 14 | Keine Zeitzonen-/Cron-Bibliothek im Core, in den Plugins oder im TE-Image; `zoneinfo` ist überall verfügbar. | eigene Prüfung; TE §10.8 | Eigener 5-Felder-Cron-Parser in `core.api.maintenance`, keine neue Abhängigkeit. Template-Engine-Linter prüft nur Syntax (stdlib). |
| 15 | Mehrere Plugins liegen noch im Layout `app/controller/` statt `app/logic/` (Orchestrator, Monitoring, Server Manager). | alle Sweeps | Neue Module in die **vorhandene** Struktur legen. Die Umbenennung ist ein eigenes Vorhaben, nicht Teil dieses Features. |

## 2. Typ-Vokabular und Schema (korrigiert)

Typen (= Entity-Registry-Kinds): `hypervisor` (physische Nodes, Controller `hardware.yml`),
`host` (VMs und LXCs, Controller `hosts.yml`), `service` (aac-Service aus `service.yml`, je
Host eine Instanz), `container` (Docker-Container; nur Anzeige, keine eigene Quelle im ersten Wurf).

### 2.1 Fensterformat (normalisiert, so liegt es auch im Bus)

```yaml
# Eingabe (service.yml / Controller)             # Normalform (Bus, Tabellen)
- weekly: {days: [sun], start: "02:00", end: "04:00", tz: Europe/Berlin}
                                                 # {"type":"weekly","days":["sun"],"start":"02:00","end":"04:00","tz":"Europe/Berlin"}
- once:   {start: "2026-11-02T22:00+01:00", end: "2026-11-03T02:00+01:00", note: "Storage-Umzug"}
                                                 # {"type":"once","start":"…","end":"…","note":"…"}
- cron:   {expr: "0 2 1 * *", duration_minutes: 180, tz: Europe/Berlin}
                                                 # {"type":"cron","expr":"0 2 1 * *","duration_minutes":180,"tz":"Europe/Berlin"}
```

Regeln: `weekly.end < start` bedeutet „über Mitternacht". `tz` ist optional; fehlt es, gilt die
Site-Zeitzone (`site_vars.timezone`, im Controller vorhanden), sonst UTC. `days` akzeptiert
`mon…sun` und `0…6` (0 = Montag, ISO). Cron: 5 Felder, `*`, Listen, Bereiche, Schritte, Monats-
und Wochentagsnamen, `0`/`7` = Sonntag, keine `@`-Makros.

### 2.2 `service.yml`

```yaml
maintenance:
  windows: [...]                 # leer/fehlt = erbt Host → Stage-Default → Site-Default → aus
  deploy_policy: window          # window | immediate; fehlt = prod → window, sonst immediate
incidents:                       # fehlt = incident_defaults.service der Stage
  enabled: true
  offline_minutes: 10            # 0 = keine Regel
  severity: high                 # critical | high | medium | low
  auto_resolve: true
stage_overrides:
  prod:
    maintenance: {...}           # ersetzt den ganzen Block für diese Stage (Listen werden ersetzt)
```

### 2.3 iac-controller

```yaml
# sites/<site>/stages/<stage>/hosts.yml           # je Host (VM/LXC), neben terraform:/services:
hosts:
  docker-hydra:
    maintenance:
      windows: [...]
      inherit: true              # Services auf diesem Host erben, wenn sie nichts eigenes haben
    incidents: {...}             # optional, Host-Regel
    services:
      - name: aac-grafana
        config: {}
        maintenance: {...}       # optional: Fenster dieser Service-Instanz auf diesem Host
                                 # (ServiceDefinition braucht extra="allow", siehe plan-iac-orchestrator)

# sites/<site>/hardware.yml                       # je Hypervisor
hardware_hosts:
  hydra:
    maintenance: {windows: [...]}

# sites/<site>/stages/<stage>/stage_vars.yml      # UNTER stage_vars:
stage_vars:
  maintenance_default:
    enabled: true                # der Default-Schalter (Orchestrator-UI schreibt diese Zeile zurück)
    windows: [...]
  incident_defaults:
    service:    {enabled: true,  offline_minutes: 10, severity: high,     auto_resolve: true}
    host:       {enabled: true,  offline_minutes: 5,  severity: critical, auto_resolve: true}
    hypervisor: {enabled: true,  offline_minutes: 5,  severity: critical, auto_resolve: true}
    container:  {enabled: false}

# sites/<site>/site_vars.yml                      # UNTER site_vars: (gilt für hardware_hosts, die keine stage_vars sehen)
site_vars:
  timezone: Europe/Berlin        # existiert bereits
  maintenance_default: {...}
  incident_defaults: {...}
```

Auflösung (spezifisch gewinnt, Listen werden ersetzt):

- Fenster Service: `service.yml` (+ `stage_overrides[stage]`) → Controller `services[].maintenance`
  → Host `maintenance` (nur wenn `inherit` nicht `false`) → Stage `maintenance_default` (wenn
  `enabled`) → Site `maintenance_default` → keins.
- Fenster Host/Hypervisor: eigenes `maintenance` → Stage-Default (Host) bzw. Site-Default
  (Hypervisor) → keins.
- Incident-Regel: eigener Block → `incident_defaults[<kind>]` der Stage → der Site → aus.
- `deploy_policy`: Service → Stage-Default (`prod` → `window`, sonst `immediate`).

Die Stage eines Services ist die **Controller-Stage des Zielhosts**, nicht der Branch.

## 3. Bus-Vertrag (final, Payloads)

Transportregel: `maintenance:*` und `monitoring:outage_*` werden **lokal** emittiert (kein
`cluster=True`). Begründung: Plugins mit `placement=any` laufen auf jeder Instanz, der lokale
Konsument des Emitters schreibt in die gemeinsame DB; so verarbeitet genau eine Instanz jedes
Event. Konsumenten mit In-Memory-Cache (Monitoring) invalidieren ihre Peers mit ihrem **eigenen**
Cluster-Broadcast. Handler sind trotzdem idempotent (Unique-Keys, delete+insert je `source` in
einer Transaktion), damit ein späterer Wechsel auf `cluster=True` ohne Schaden möglich ist.
Zeitstempel im Bus sind ISO-8601 mit Offset (UTC bevorzugt). Topics stehen in `permissions.emit`
bzw. `permissions.subscribe` der Manifeste.

| Topic | Emitter | Payload |
|---|---|---|
| `maintenance:windows_published` | Orchestrator (Runner, nach Boot und jeder `NativeGenerateStage`), Monitoring (manuelle Fenster, `source: "monitoring"`) | `{source, published_at, replaces: true, items: [{ci_id, kind, name, host_ci_id?, windows: [Normalform…], inherited_from: null \| "host" \| "stage_default" \| "site_default", deploy_policy?: "window" \| "immediate", incidents?: {enabled, offline_minutes, severity, auto_resolve}}]}` |
| `maintenance:override_started` | Orchestrator (jeder ausgeführte Deploy-Job, „Jetzt ausführen"), Monitoring (manuelles Override), später ITSM | `{override_id, ci_ids: [uuid…], until, reason, source, started_at, linger_minutes: 15}` |
| `maintenance:override_ended` | dito | `{override_id, source, ended_at, linger_minutes: 15}` |
| `monitoring:outage_persisted` | Monitoring (Prober-Instanz, einmal je Ausfall) | `{outage_id, ci_id, kind, name, host_ci_id?, monitor_id, since, persisted_at, duration_s, rule: {offline_minutes, severity, auto_resolve}, error?}` |
| `monitoring:outage_recovered` | Monitoring | `{outage_id, ci_id, since, recovered_at, duration_s}` |

`outage_id` ist deterministisch `"<ci_id>:<since als ISO-UTC>"`; ITSM nutzt es als
`external_id`. `replaces: true` heißt: der Konsument ersetzt **alle** Einträge dieser `source` in
einer Transaktion. Ein Item ohne `ci_id` wird vom Emitter nicht gesendet. Ein Konsument ignoriert
unbekannte Felder und unbekannte `ci_id`s.

Override-Semantik beim Konsumenten: aktiv von `started_at` bis `until`; nach `override_ended`
noch bis `ended_at + linger_minutes`. Abgelaufene Overrides werden beim Lesen ignoriert und
gelegentlich gelöscht; kein Timer nötig.

## 4. Architekturentscheidungen (Fable, zur Review)

1. **Entscheidung im Job („Park")** statt Snapshot auf der Control Plane (Befund 6). Ablauf:
   Webhook → `QUEUED` wie heute → Runner claimt → `CloneServiceRepoStage` → neue
   `MaintenancePolicyStage` → bei `window` und nicht im Fenster: `park(job, not_before =
   nächster Fensterbeginn, window_ci_ids)` → Status `SCHEDULED`, Claim freigegeben, kein
   Endstatus. Fällig: `claim_next` nimmt `SCHEDULED` mit `not_before <= now` → erneuter Clone
   (frische `service.yml`) → Policy-Stage sieht `not_before` erreicht → läuft. Kein Fenster in
   30 Tagen → Warnung und sofort ausführen (Deploys dürfen nicht ewig hängen).
2. **Jeder ausgeführte Deploy ist ein Override** (`deploy_marks_maintenance`, Default an): nicht
   nur „Jetzt ausführen", auch `immediate`-Deploys und Katalog-Button. Begründung: jeder Deploy
   startet Container neu und würde sonst `down` erzeugen. „Jetzt ausführen" = SCHEDULED → QUEUED
   promoten; das Override entsteht dann beim Lauf.
3. **Service-CIs und `runs_on`** kommen vom Orchestrator (Befund 2). Identifier
   `iac.service = "<host>/<service.name>"` (Registry-Katalog erweitern, Priorität 20),
   `display_name = "<service> @ <host>"`. `host_ci_id` reist zusätzlich in jedem Event mit.
4. **`ci_id` ins Monitoring über den bestehenden HTTP-Inventory-Sync** (Befund 3), nicht über ein
   weiteres Bus-Abo. `config_hash` schließt `ci_id` ein.
5. **Outage-Uhr:** `down_since` wird im selben Commit wie der Heartbeat gesetzt; läuft ein Fenster
   oder Override, wird `suppressed_until = Fensterende` fortgeschrieben; effektive Ausfallzeit =
   `now - max(down_since, suppressed_until)`. Einmalige Zustellung über bedingtes `UPDATE … WHERE
   state='open'`. Nur die probende Instanz wertet aus (Lease oder `owns()` im Shard-Modus).
6. **Monitoring-Zustand `MAINTENANCE`** ersetzt nur `DOWN` (ein erfolgreicher Probe bleibt `UP`).
   Eigene Aggregatspalte `maintenance_samples`; Uptime-Nenner = `total - paused - maintenance`.
   Rangfolge für Gruppen: `DOWN > UNKNOWN > MAINTENANCE > UP > PAUSED`. Neues Farbtoken
   `--lx-state-maintenance` (Core-Whitelist + Shell + Default-Theme), Fallback `--lx-state-paused`.
7. **ITSM-Dedupe** über `source="monitoring"`, `external_id=outage_id` (cluster-sicher durch
   `UNIQUE`), plus Abfrage „offener Incident mit `affected`-Link auf `ci_id` und
   `source=monitoring`" → bei Treffer Kommentar statt neuem Record. Ein erneuter Ausfall nach
   `resolved` erzeugt einen neuen Incident (kein Reopen). Host-CI als `related`. Auto-Resolve mit
   `resolution_code: auto_recovered` (Katalog), Pflichtnotizen generiert. Benachrichtigung
   `record_created` für automatisch angelegte P1/P2.
8. **Read-Models** in Registry und Server Manager als **eigene Tabellen**, Auswertung
   (aktuell/nächstes Fenster, abgelaufene Overrides) **beim Lesen** über die Core-Bibliothek,
   keine Pro-Entity-Events bei Massen-Publish.
9. **Default-Schalter** als Pipeline-Typ `maintenance_default_toggle` (Runner) mit zielgenauer
   Textänderung in `stage_vars.yml`, Commit-Message `ci: maintenance default <site>/<stage> on|off`
   (Schleifenschutz um dieses Präfix erweitern), danach Re-Sync und Re-Publish.
10. **Statusvertrag CI** (Befund 7): `GET …/gitlab/status?accept=scheduled` → `{"status":
    "scheduled", "not_before": …}`; ohne `accept` → `status: "success"`, `job_status: "SCHEDULED"`.
    Beim Zusammenfassen eines `SCHEDULED`-Jobs wird `start_time = now` gesetzt, damit die zweite
    CI den Job über `since_epoch` findet.

## 5. Entscheidungen für Marvin (mit meinem Default)

| # | Frage | Default, wenn keine Antwort |
|---|---|---|
| A | Typ-Vokabular ohne `vm` (`hypervisor \| host \| service \| container`)? | ja |
| B | Jeder ausgeführte Deploy erzeugt ein Override (nicht nur „Jetzt ausführen")? | ja, Setting `deploy_marks_maintenance` |
| C | Push-Recht für den Orchestrator auf `iac-controller/main` (Credential der Rolle `iac_controller`, Branch-Protection)? | Schalter wird gebaut, in der UI deaktiviert bis Push-Recht da ist |
| D | CI-Template-Umbau (`?accept=scheduled`) durch die IaC-Session in P6; bis dahin Kompatibilitätsmodus (`success` bei `SCHEDULED`)? | ja |
| E | Server Manager: nur Badge + Filter, Detailansicht später? | ja |
| F | ITSM: neuer Incident je Ausfall, kein Reopen eines `resolved`? | ja |
| G | Zeitzone ohne `tz`: Site-`timezone`, sonst UTC? | ja |
| H | Kein Fenster in 30 Tagen → sofort deployen mit Warnung? | ja, Setting `schedule_horizon_days` |

## 6. Phasen, Abhängigkeiten, Versionen

| Phase | Teilplan | Version | Abhängigkeit |
|---|---|---|---|
| P1 | `plan-core.md`: `core.api.maintenance`, API 1.13, Token, Doku | core 1.11.0, ui 1.8.0 | keine |
| P2 | `plan-iac-orchestrator.md` | orchestrator 1.6.0 | P1; Registry-Scheme `iac.service` aus P5a (Reihenfolge: P5a vor P2 ausrollen) |
| P3 | `plan-monitoring.md` | monitoring 1.7.0 | P1; `ci_id` im Inventory-Sync aus P2 |
| P4 | `plan-itsm.md` | itsm 0.3.0 | P3-Payloads (im Vertrag fixiert, parallel baubar) |
| P5a | `plan-entity-registry.md` | entity-registry 0.3.0 | P1 (Guard) |
| P5b | `plan-server-manager.md` | server-manager 1.4.0 | P1 (Guard) |
| P6 | `plan-iac-workspace.md` (Übergabe an die IaC-Session) | template-engine 0.0.10, controller-Doku | P2-Statusvertrag |
| P7 | Durchstich (Abschnitt 7) | – | alle |

Bauen: P1 zuerst (Fable selbst), dann P2–P5 parallel als Subagenten mit je einem Teilplan,
Rollout-Reihenfolge Core → Registry → Orchestrator → Monitoring → ITSM → Server Manager → k3s-Roll.
Jeder Teilplan endet mit Tests im Dev-Core, CHANGELOG, Doku (DE), Version + Tag, ohne Push bis
zur Freigabe.

## 7. P7-Durchstich (Dev-Bench)

1. Controller-Fixture: Stage `dev` mit `maintenance_default` (Fenster jetzt+2 min bis +10 min),
   Host mit eigenem Fenster, `incident_defaults`. Service-Fixture `service.yml` mit
   `deploy_policy: window` und `incidents.offline_minutes: 1`.
2. Webhook `POST /api/iac/deploy/service/<svc>/gitlab` → Job `QUEUED` → Runner klont → `SCHEDULED`
   mit `not_before`; Status-Endpunkt liefert `success`/`SCHEDULED` bzw. `scheduled` mit
   `?accept=scheduled`; zweiter Webhook wird zusammengefasst.
3. „Jetzt ausführen" in der UI → Job läuft → `override_started` → Monitoring zeigt die Monitore
   des Hosts/Services als `maintenance`, Registry und Server Manager zeigen das Badge.
4. Job-Ende → `override_ended` → nach `linger_minutes` wieder normal.
5. Service stoppen (außerhalb Fenster) → nach `offline_minutes` `outage_persisted` → ITSM-Incident
   mit CI-Links Service (`affected`) und Host (`related`), Priorität aus Severity; Service starten →
   `outage_recovered` → Incident `resolved` (`auto_recovered`).
6. Dasselbe innerhalb des Fensters → kein Incident, Zustand `maintenance`.
7. Default-Schalter in der Orchestrator-UI → Commit im Controller-Fixture-Repo → Re-Sync →
   Fenster verschwinden/erscheinen im Monitoring.
