# Teilplan P2: IaC Orchestrator 1.6.0 — Fenster auflösen, geplante Deployments, Overrides

Entscheidung C (2026-10-18): der Orchestrator bleibt Ausführung. **Kein Default-Schalter mit
Git-Rückschreiben** in diesem Plugin; er zeigt den Default nur an. Entscheidung H: ein
`window`-Deploy ohne Fenster im Horizont läuft sofort und emittiert `maintenance:window_missing`.
Entscheidung A: Kinds sind offen (`core.api.maintenance.is_valid_kind`), keine feste Liste.

Ausführung: Fable-Subagent. Grundlage: `analysis-iac-orchestrator.md`, `00-ergebnis-p0.md`
(Abschnitte 2–4). Repo `plugins/lyndrix-plugin-iac-orchestrator`, Layout bleibt
`app/controller/` (keine Umbenennung). Voraussetzung: Core 1.11.0 (API 1.13) läuft im Dev-Core;
Registry 0.3.0 kennt das Scheme `iac.service` (plan-entity-registry.md, vorher ausrollen; bis
dahin antwortet die Registry mit `ok:false` für Service-Items → `ci_id=None`, kein Fehler).

## 1. Manifest und Versionen

- `entrypoint.py`: `version="1.6.0"`, `min_core_version="1.11.0"`; `app/api.py:35`
  `PLUGIN_VERSION="1.6.0"` (Test hält beide gleich).
- `permissions.emit` + `maintenance:windows_published`, `maintenance:override_started`,
  `maintenance:override_ended`, `maintenance:window_missing`, `entity_registry:relate`.
- Keine neue Permission (der Default-Schalter ist nur lesend, `api:read` reicht).
- CHANGELOG, `documentation.md` (DE: Abschnitte „API-Referenz" und „Events" ergänzen, neuer
  Abschnitt „Wartungsfenster und geplante Deployments"), `ai/INSTRUCTIONS.md` (Scheduled
  Deploys, Run now = Override, Default-Fenster nur Anzeige: Änderung im Controller-Repo).

## 2. Auflösung und Veröffentlichung (`app/controller/maintenance_resolver.py`, neu)

Läuft nur auf Runnern (Working Trees). Pure Funktionen plus ein dünner Publisher.

```python
def load_tree(inventory_root: Path, site: str, stage: str) -> dict
    # iac_core.app.services.loader.load_configuration(...) — roher Merge, Werte sind literal.
    # Zusätzlich sites/<site>/site_vars.yml für hardware_hosts (bekommen keine stage_vars).
def scan_service_ymls(dirs: Iterable[Path]) -> dict[str, ServiceMaint]
    # rglob("service.yml") wie engine._load_service_docker_names (engine.py:956-1014); je Datei
    # try/except mit Log; liest service.name, maintenance, incidents, stage_overrides[*].maintenance/incidents
def resolve(site, stage, tree, services: dict[str, ServiceMaint], ci: CiMap, *, default_tz) -> list[Item]
    # Item = {ci_id, kind, name, host_ci_id?, windows: [Normalform], inherited_from, deploy_policy?, incidents?}
    # Kette aus 00-ergebnis-p0.md Abschnitt 2.3; Listen ersetzen; tz-Default = site_vars.timezone or "UTC";
    # Validierung über core.api.maintenance.parse_windows — ungültige Blöcke: Log + Block ignorieren, nie abbrechen.
def service_policy(items, service_name, hosts) -> PolicyDecision
    # deploy_policy, windows des ersten Zielhosts (sortiert), Hinweis wenn Hosts abweichen
```

Publisher (`engine.py`, neben `emit_iac_inventory_updated`, Zeilen 1271-1295):

1. `_attach_ci_ids` (1196-1243) erweitern: Items für Services (`entity_type="service"`,
   `identifiers={"iac.service": f"{host}/{service}"}`, `display_name=f"{service} @ {host}"`,
   `attributes={"host": host, "site":…, "stage":…}`, `source="iac-orchestrator"`). Ergebnis in
   `self._ci_map: dict[tuple, str]` (`("host", name)`, `("service", host, name)`), eine
   `ctx.request` mit Timeout 10 s wie heute; Chunking bei > 500 Items.
2. Danach `entity_registry:relate` je Service: `{from_uuid: service_ci, to_uuid: host_ci,
   relation: "runs_on", source: "iac-orchestrator"}` (fire-and-forget).
3. `_build_monitoring_inventory_payload` (1016-1089): `ci_id` an `hosts[]` und `services[]`,
   `host_ci_id` an `services[]`, aus `self._ci_map`. **Reihenfolge in `NativeGenerateStage`
   (143-153) ändern:** erst `_attach_ci_ids`, dann Monitoring-Sync, dann
   `emit_iac_inventory_updated`, dann `publish_maintenance()`. Beim Boot
   (`entrypoint.py:302-307`) dieselbe Reihenfolge.
4. `publish_maintenance()`: `resolve` je Site/Stage → ein `ctx.emit(TOPIC_WINDOWS_PUBLISHED,
   {source:"iac-orchestrator", published_at, replaces:true, items})` (lokal) → Snapshot
   `iac_orchestrator_state` Key `maintenance_effective` (Muster `publish_repo_snapshots`,
   `api.py:501-522`) für die UI auf allen Instanzen.
5. `_build_inventory_hosts` (1161-1194): `maintenance` und `incidents` aus `host_data` mitnehmen
   und `ci_id` wie bisher.

`iac_core/app/validate/models.py:6-12` `ServiceDefinition`: `model_config = ConfigDict(extra="allow")`,
damit `services[].maintenance` im Controller überlebt (Test ergänzen).

## 3. Queue: `SCHEDULED`, Park, Promote, Zusammenfassen (`app/controller/queue.py`, `app/model/models.py`)

- Spalten (`models.py:11-36`, `queue.py:148-155 wanted`): `not_before DATETIME NULL`,
  `window_ci_ids LONGTEXT NULL` (JSON-Liste), `override_id VARCHAR(64) NULL`,
  `schedule_reason VARCHAR(255) NULL`.
- `STATUS_SCHEDULED = "SCHEDULED"`. Zeiten wie die übrigen Spalten **naiv in der Prozessuhr**
  (`datetime.now()`): Helfer `to_queue_clock(aware) -> naive` und `from_queue_clock(naive) -> aware`.
- `claim_next` (224-266): Kandidaten `status='QUEUED' OR (status='SCHEDULED' AND not_before <=
  now)`, Sortierung `not_before NULLS FIRST, id`; bedingtes UPDATE auf `status IN ('QUEUED','SCHEDULED')`.
- `park(job_id, *, not_before, window_ci_ids, reason) -> bool`: bedingtes UPDATE
  `RUNNING → SCHEDULED WHERE owner_instance = me`, setzt die drei Felder, löscht
  `owner_instance/claimed_at/heartbeat_at`, `progress=0`, `current_step="Scheduled"`.
- `promote(job_id, *, override: bool) -> bool`: `SCHEDULED → QUEUED`, `not_before=NULL`,
  Payload-Flag `force=true` setzen; Aufrufer emittiert `iac:job_queued` (`service.py:80-90`).
- `enqueue` (184-221): existiert ein `SCHEDULED`-Job mit gleichem `dedupe_key` → **zusammenfassen**:
  `payload` ersetzen, `start_time = now`, Rückgabe `(job_id, coalesced=True)`; `QUEUED`/`RUNNING`
  bleibt `DuplicateJob`. `_conflicts` und `dedupe_key_for` entsprechend.
- `abort_queued` (387-406): `SCHEDULED` nur mit `include_scheduled=True`. `live_summary`
  (457-487): `scheduled` zählen. `database.py:287-315 clear_all_jobs`: `SCHEDULED` behalten.
- `database.py:148-191 get_latest_job_for_pipeline_type`: `SCHEDULED`-Jobs unabhängig vom
  Zeitfenster bevorzugen (ein geplanter Job ist der aktuelle Stand).
- Orphan-Sweep: `SCHEDULED` hat keinen Owner, wird nicht als verwaist gezählt.

## 4. Engine: Policy-Stage, Park, Overrides (`app/controller/engine.py`)

- Webhook-Payload (`controller/api.py:647-653`): `trigger: "gitlab-ci"` ergänzen, `manual`
  bleibt. Policy gilt nur für `trigger in ("gitlab-ci", "webhook")`; UI-Deploys (`trigger="ui"`)
  und `force=true` laufen sofort.
- `SingleServiceStrategy.build_stages` (660-696): nach `CloneServiceRepoStage` neue
  `MaintenancePolicyStage(svc_name, svc_branch, payload)`:
  1. überspringen, wenn `force` oder Trigger nicht Webhook oder Job `not_before` gesetzt und
     erreicht (dann nur Overrides vorbereiten);
  2. `scan_service_ymls([services_dir/svc])` + `load_tree` für die Stage der Zielhosts (Hosts
     aus Controller `hosts[].services[].name == svc`; Stage = Controller-Stage, Branch nur
     Fallback wie 666-672) → `service_policy`;
  3. `deploy_policy == "window"` und `current_interval` leer → `next_start` innerhalb
     `schedule_horizon_days`; gefunden → `queue.park(...)` und `raise JobParked(not_before)`;
     nicht gefunden → Warnung ins Joblog, weiter, und (Setting
     `maintenance_report_missing_window`) `ctx.emit(TOPIC_WINDOW_MISSING, {event_id:
     make_window_missing_id(service_ci, job_id), ci_id: service_ci, kind: "service", name,
     host_ci_id, site, stage, horizon_days, job_id, source: "iac-orchestrator", detected_at,
     reason: "deploy_policy window, no window within <n> days"})` je Zielhost-Service-CI (lokal;
     ohne Service-CI kein Event, nur Log);
  4. Ergebnis (`window_ci_ids` = Service-CIs der Zielhosts + Host-CIs) am Context für Schritt 5.
- `run_pipeline` (1346-1660): `except JobParked` vor dem allgemeinen Handler: Log
  `[SYSTEM] Deployment scheduled for <iso> (<reason>)`, `parked=True`; im `finally` bei
  `parked` **kein** `update_job(final_status)`, Zähler/Keys wie bei Abort freigeben, kein
  `override_ended`. `_skip_claimed` unverändert.
- Overrides (`deploy_marks_maintenance`, Setting): direkt nach der Policy-Stage für
  `single_service` (alle Trigger): `override_id = f"iac-job-{job_id}"`, `until = now +
  max_override_minutes`, `ctx.emit(TOPIC_OVERRIDE_STARTED, {...linger_minutes})`, `override_id`
  in der Job-Zeile. `override_ended` im `finally` (1646-1660 und 1718-1723), in
  `queue.mark_failed/fail_orphans` (336-364) und im Abort-Pfad (`api.py:1130-1205`,
  `engine.abort_job` 1932) über einen Service-Callback `on_job_terminal(job_row)`; Emits laufen
  über `ctx.emit` (thread-sicher seit Core 1.7).
- Fehlt `_ci_map` für einen Zielhost (Registry nicht erreichbar): kein Override-Event, Log.

## 5. Default-Fenster: nur Anzeige (Entscheidung C)

- Kein Pipeline-Typ, keine Schreibstage, kein Push. Der Default kommt aus dem Snapshot
  `maintenance_effective` (je Site/Stage `{enabled, windows, source_file}` aus
  `stage_vars.maintenance_default` bzw. `site_vars.maintenance_default`).
- Schleifenschutz vorbereiten: `_should_trigger_orchestrator` (`api.py:252-278`) und der
  Webhook-Sync ignorieren Deploy-Trigger für Commits mit Präfix `ci: maintenance` (Sync läuft
  weiter). Das ist das Präfix, das der künftige SSoT-Manager-Schalter verwendet
  (`00-ergebnis-p0.md` Abschnitt 8).
- UI-Hinweis an der Karte: „Änderung im iac-controller (`stage_vars.yml`, Block
  `maintenance_default`)" mit Pfad der Datei.

## 6. Settings (`app/controller/settings_schema.py` `_build_fields`, Kategorie „Maintenance")

| Key | kind | Default |
|---|---|---|
| `maintenance_deploy_marks_maintenance` | bool | true |
| `maintenance_max_override_minutes` | int | 240 |
| `maintenance_linger_minutes` | int | 15 |
| `maintenance_schedule_horizon_days` | int | 30 |
| `maintenance_report_missing_window` | bool | true (emittiert `maintenance:window_missing`) |

Zugriff über die bestehende Vault-/Env-Mechanik (`config.py`, Env `PLUGIN_IAC_ORCHESTRATOR_*`
hat Vorrang).

## 7. API (`app/api.py`, Handler in `controller/api.py`)

| Methode | Pfad | Perm | Inhalt |
|---|---|---|---|
| GET | `/schedules` | read | `SCHEDULED`-Jobs: id, service, branch, `not_before` (ISO), `window_ci_ids`, `schedule_reason`, `start_time` |
| POST | `/schedules/{job_id}/run-now` | write | `promote(override=True)` + `iac:job_queued` (cluster) |
| DELETE | `/schedules/{job_id}` | write | geplanten Job abbrechen (`ABORTED`) |
| GET | `/maintenance-windows?site=&stage=&kind=&q=` | read | Snapshot `maintenance_effective` plus `effective_state` je Item (Core-Bibliothek, `now`) |
| GET | `/maintenance-default` | read | je Site/Stage `{enabled, windows, source_file}` aus dem Snapshot (nur lesend) |

Öffentlich (`webhook_router.py`, `api.py:659-694 do_get_service_status`): Query `accept`
(Komma-Liste). `SCHEDULED` → mit `accept=scheduled`: `{"status":"scheduled","job_status":
"SCHEDULED","not_before":…}`; ohne: `{"status":"success","job_status":"SCHEDULED","not_before":…}`.
`QUEUED` → `running`. Die bestehenden `/maintenance/*`-Pfade bleiben (Plugin-Pflege).

## 8. UI (`src/ui/PluginApp.tsx`, `src/ui/lib/api.ts`, `locales/`)

- Neuer Tab `maintenance` („Wartung") in `TabId` (1301), `NavTile`, `ViewHeader`. Drei Karten:
  **Geplante Deployments** (Tabelle, Buttons „Jetzt ausführen" mit Confirm wie `abort()`
  1469-1483, „Abbrechen"), **Effektive Fenster** (Filter Site/Stage/Typ, Badge „aktiv bis",
  „nächstes"), **Default-Fenster** (je Site/Stage Zustand an/aus und Fenster, nur Anzeige, mit
  Hinweis auf die Controller-Datei; kein Toggle).
- `StatusBadge`/`statusColor` (56-86): `SCHEDULED` → `--lx-state-maintenance` mit Fallback
  `--lx-state-paused`; `ActivePipelines` zeigt geplante getrennt („geplant: n").
- `api.ts`: `listSchedules`, `runNow`, `cancelSchedule`, `maintenanceWindows`,
  `maintenanceDefault`. Locales de/en (i18next-Dialekt).
- NiceGUI-Seiten unverändert (React ist Ziel).

## 9. Tests (`tests/`, im Dev-Core: `docker cp` + `PYTHONPATH=/app pytest -q tests`)

- `test_maintenance_resolver.py`: Vererbungskette (Service > Controller-Service > Host >
  Stage-Default > Site-Default > keins), `inherit: false`, `stage_overrides.prod`, Liste ersetzt,
  tz-Default aus `site_vars.timezone`, ungültiger Block wird ignoriert und geloggt,
  `incident_defaults` je Kind, `deploy_policy` Default nach Stage, Hypervisor über Site.
- `test_queue.py` erweitern: park/promote, claim nimmt fälliges `SCHEDULED`, nicht fälliges
  bleibt liegen, Zusammenfassen setzt `start_time`, `abort_queued` lässt `SCHEDULED`,
  `clear_all_jobs` behält `SCHEDULED`.
- `test_policy_stage.py`: Entscheidung mit Fake-Uhr (in Fenster → weiter; außerhalb → Park mit
  `not_before`; kein Fenster im Horizont → weiter mit Warnung **und** genau ein
  `window_missing`-Emit mit deterministischem `event_id`, kein Emit bei Setting aus oder ohne
  Service-CI; `force` → weiter; `trigger=ui` → weiter); `JobParked` lässt `update_job` aus.
- `test_inventory_publish.py` erweitern: Service-Items in `identify`, `relate`-Emits, `ci_id`
  und `host_ci_id` im Monitoring-Payload, `windows_published` einmal mit `replaces:true`.
- `test_status_mapping.py`: `accept=scheduled` vs. Kompatibilitätsmodus.
- Schleifenschutz: Controller-Commit mit Präfix `ci: maintenance` löst keinen Deploy aus, der
  Sync läuft.
- `iac_core/tests`: `ServiceDefinition` behält `maintenance`.

## 10. Abnahme (Dev-Bench)

Siehe `00-ergebnis-p0.md` Abschnitt 7, Schritte 2–4, 7 und 8. Zusätzlich: ein zweiter Webhook für
denselben Service wird zusammengefasst; nach einem Runner-Neustart bleibt der `SCHEDULED`-Job
erhalten und wird fällig ausgeführt. `ruff`, `black --check`, `mypy` sauber; Bundle neu gebaut
(`ui_static/ui_bundle.js`). Commit auf `feature/maintenance-windows`, Tag erst nach Freigabe
(`v1.6.0` und `1.6.0`).

## 11. Bekannte Grenzen (dokumentieren)

- Ein Service auf mehreren Hosts: das Fenster des ersten Zielhosts (sortiert) entscheidet;
  abweichende Hosts werden im Joblog genannt.
- Services, die der Orchestrator noch nie geklont hat, erben Host/Stage-Default (kein eigenes
  `service.yml` auf dem Runner).
- Der Default-Schalter ist hier nur lesbar; Ein/Aus wird im Controller-Repo geändert, bis der
  SSoT Manager das Rückschreiben übernimmt (`00-ergebnis-p0.md` Abschnitt 8).
- `window_missing` wird je Deploy-Job emittiert; die Deduplizierung (ein offener Task je CI)
  liegt im ITSM. Ohne ITSM verbleibt nur die Joblog-Warnung.
