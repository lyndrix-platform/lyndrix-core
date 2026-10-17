# Teilplan P4: ITSM 0.3.0 — Incidents aus Outage-Events, Auto-Resolve

Ausführung: Fable-Subagent. Grundlage: `analysis-itsm.md`, `00-ergebnis-p0.md` (Abschnitt 3
Payloads, Abschnitt 4 Punkt 7). Repo `plugins/lyndrix-plugin-itsm` (kanonisches Layout
vorhanden). Keine Abhängigkeit zur Core-Bibliothek; `min_core_version` bleibt `1.4.0`.
Baubar parallel zu P3, weil die Payloads im Vertrag fixiert sind.

## 1. Manifest und Katalog

- `entrypoint.py:76` `version="0.3.0"`. `permissions.subscribe` (110-125) +
  `monitoring:outage_persisted`, `monitoring:outage_recovered`, `itsm:record_created` (eigener
  Broadcast, für die neue Benachrichtigung).
- `catalog/record_types/incident.yml`: `resolution_code` (96-104) um `auto_recovered` erweitern;
  Felder `monitor_rule` (string) und `outage_id` (string) als Custom-Felder deklarieren, damit
  der Katalog sie validiert (`service.py:400`). Kategorie `monitoring` (40) und Subtype `outage`
  (15) existieren.
- CHANGELOG, `documentation.md` (DE: neuer Abschnitt „Automatische Incidents aus dem
  Monitoring", Events-Tabelle), Locales de/en (Texte für Journal und Notification).

## 2. Logik (`app/logic/monitoring_incidents.py`, neu; `entrypoint.py` bleibt dünn)

```python
SEVERITY_TO_IMPACT_URGENCY = {"critical": (1, 1), "high": (1, 2), "medium": (2, 2), "low": (3, 2)}
SOURCE = "monitoring"
AUTHOR = "monitoring"

class MonitoringIncidents:
    def __init__(self, service: ItsmService, ctx): ...
    async def handle_outage_persisted(self, payload: dict) -> str | None   # returns record uuid
    async def handle_outage_recovered(self, payload: dict) -> None
```

`handle_outage_persisted`:

1. Pflichtfelder `outage_id`, `ci_id` (36 Zeichen), `rule` prüfen; sonst Log + `itsm:ingest_failed`.
2. **Dedupe 1 (Ausfall):** `service.find_by_external(SOURCE, outage_id)` → existiert → nichts
   tun (Event doppelt zugestellt).
3. **Dedupe 2 (CI):** `service.find_open_for_ci(ci_id, record_type="incident", source=SOURCE)`
   — Join `Record × CiLink` (`entity_uuid == ci_id`, `role == "affected"`), `Record.state` in den
   Zuständen mit Kind `open` laut Katalog (`new`, `in_progress`, `on_hold`; **nicht**
   `closed_at IS NULL`, siehe analysis-itsm §3). Treffer → `add_journal(ref, "work_note",
   "Erneuter/weiterer Ausfall gemeldet: <name>, seit <since>, Dauer <duration>", AUTHOR)`,
   `custom_fields.outage_id` unverändert lassen, fertig.
4. Sonst `create_record(data, author=AUTHOR)` mit `record_type="incident"`,
   `subtype="outage"`, `category="monitoring"`, `impact/urgency` aus
   `SEVERITY_TO_IMPACT_URGENCY[rule.severity]` (unbekannt → `high`), `title =
   f"{name}: nicht erreichbar seit {since_local}"`, `description` (Monitor, CI, Host, Regel,
   Dauer, letzter Fehler), `tags=["auto", "monitoring", kind]`, `custom_fields={"monitor_rule":
   json, "outage_id": outage_id, "ci_kind": kind}`, `source=SOURCE`, `external_id=outage_id`,
   `ci_links=[{"entity_uuid": ci_id, "role": "affected"}] + ([{"entity_uuid": host_ci_id, "role":
   "related"}] if host_ci_id)`. CI-Links über `resolve_ci_links` (Registry-Namen-Cache,
   Fallback roher UUID-Link bei Registry-Ausfall). `IntegrityError` auf
   `uq_itsm_source_ext` (Instanz-Race) → als Erfolg behandeln (Record des Gewinners lesen).
5. `itsm:record_created` wird vom Service emittiert; die neue Notification (Abschnitt 4)
   greift dort.

`handle_outage_recovered`:

1. `find_by_external(SOURCE, outage_id)` → fehlt → Log, fertig.
2. State `resolved`/`closed`/`cancelled` → nichts.
3. `rule.auto_resolve` aus `custom_fields.monitor_rule` (nicht aus dem Payload, der kennt die
   Regel nicht) → `True`: `transition(ref, "resolved", fields={"resolution_code":
   "auto_recovered", "resolution_notes": f"Automatisch gelöst: Monitor wieder erreichbar um
   {recovered_at}, Ausfalldauer {duration}."}, note=..., author=AUTHOR)`.
   `TransitionError` (z. B. aus `on_hold`) → Fallback Kommentar. `VersionConflict` → einmal
   neu lesen und wiederholen, dann aufgeben mit Log.
4. `auto_resolve == False` → `add_journal(ref, "comment", "Monitor wieder erreichbar …", AUTHOR)`
   (löst `record_commented` aus).

Service-Ergänzungen (`app/logic/service.py`): `find_by_external(source, external_id)`,
`find_open_for_ci(ci_id, *, record_type, source=None)`, `open_state_names(record_type)` aus
dem Katalog (`catalog.py` kennt State-Kinds). Alles synchron, Aufruf per `asyncio.to_thread`
wie die übrigen Handler.

## 3. Bus-Handler (`entrypoint.py`, Muster `_on_record_create` 261-277)

```python
@ctx.subscribe("monitoring:outage_persisted")
async def _on_outage_persisted(payload): await incidents.handle_outage_persisted(payload)
@ctx.subscribe("monitoring:outage_recovered")
async def _on_outage_recovered(payload): await incidents.handle_outage_recovered(payload)
```

Fehler → `itsm:ingest_failed` (bestehendes `_fail`), nie Exception nach außen. Kein Lease:
Idempotenz kommt aus `UNIQUE(source, external_id)` (deterministisches `outage_id`) und der
CI-Abfrage; ein gleichzeitiges Doppel über Instanzen ist durch die lokale Transportregel
ohnehin die Ausnahme.

## 4. Notification für automatisch angelegte Incidents

`_notify` (`entrypoint.py:362-370`) um Endpoint `record_created` erweitern
(`notification_endpoints` 142-149); Subscriber auf `itsm:record_created`: nur wenn
`record.source == "monitoring"` **und** Priorität P1/P2 → Severity `warning`, Text
„Incident <number> automatisch angelegt: <title>", Link `/apps/lyndrix-plugin-itsm/itsm?record=<uuid>`.
Manuell angelegte Records bleiben wie heute (keine Meldung bei Anlage).

## 5. UI (optional, klein)

`RecordList` (`PluginApp.tsx:275`): Tag-Chip „auto" sichtbar machen und Filter „Automatisch
(Monitoring)" über `tag=auto`. Detail: Custom-Felder `outage_id`/`monitor_rule` rendert die
Katalog-Logik generisch. Bundle neu bauen.

## 6. Tests (`tests/test_service.py` erweitern oder `tests/test_monitoring_incidents.py` neu)

Fixture `svc` + `_RegistryCtx` (`test_service.py:25-44`, `:341-370`):

- Persisted → Incident mit P2 bei `high`, Links `affected` (Service) und `related` (Host),
  `source/external_id`, Tags, Custom-Felder.
- Persisted doppelt (gleiches `outage_id`) → ein Record.
- Zweites Persisted mit anderem `outage_id`, offener Incident am CI → Work-Note statt Record.
- Recovered mit `auto_resolve: true` → `resolved`, Code `auto_recovered`, `resolved_at` gesetzt.
- Recovered aus `on_hold` → Kommentar, Zustand bleibt.
- Recovered mit `auto_resolve: false` → Kommentar `comment`.
- Recovered für unbekanntes `outage_id` → nichts, kein Fehler.
- Erneuter Ausfall nach `resolved` → neuer Incident.
- Severity unbekannt → `high`; `ci_id` ungültig → `ingest_failed`.
- Registry nicht erreichbar (`ctx.request` → `None`) → Record mit rohen UUID-Links.

Lauf: `docker cp . lyndrix-core-dev:/tmp/itsm && docker exec -w /tmp/itsm -e PYTHONPATH=/app:/tmp/pytest-sm
lyndrix-core-dev python -m pytest -q tests`; `ruff check entrypoint.py app tests`.

## 7. Abnahme

`00-ergebnis-p0.md` Abschnitt 7, Schritte 5–6 auf der Dev-Bench mit echten Monitoring-Events.
Commit auf `feature/maintenance-windows`, Tags `v0.3.0`/`0.3.0` nach Freigabe.
