# Teilplan P5b: Server Manager 1.4.0 — Wartungs-Badge am Host

Ausführung: Fable-Subagent (kleinster Teilplan). Grundlage: `analysis-server-manager.md`,
`00-ergebnis-p0.md`. Repo `plugins/lyndrix-plugin-server-manager`, Layout bleibt
`app/controller/` + `src/ui/`. Core-Bibliothek optional (`try/except ImportError`),
`min_core_version` bleibt `0.0.6`. Entscheidung E: nur Badge, Tooltip und Filter; keine
Detailansicht in diesem Wurf.

## 1. Manifest

`entrypoint.py:78-89` `version="1.4.0"`; `permissions.subscribe` (113-122) +
`maintenance:windows_published`, `maintenance:override_started`, `maintenance:override_ended`.
`permissions.emit` unverändert. CHANGELOG, `documentation.md` (DE), Locales de/en.

## 2. Datenmodell (`app/model/models.py`, gleiche `_Base`; `create_all` in `database.py:89`)

```
server_manager_maintenance  id PK, ci_id String(36) idx, kind String(16)  -- window | override
                            source String(64), override_id String(64) NULL,
                            windows JSON NULL, inherited_from String(32) NULL,
                            until DateTime NULL, started_at DateTime NULL, ended_at DateTime NULL,
                            linger_minutes Integer NULL, reason Text NULL, updated_at
                            UNIQUE (ci_id, source, kind, override_id)
```

Events für `ci_id`s ohne Server-Record werden **gespeichert** (Reihenfolge `windows_published`
vor `register_server` ist normal); die Anzeige verbindet beim Lesen über `servers.ci_id`.

## 3. Logik (`app/controller/service.py`)

- `apply_windows_published(payload)`, `apply_override_started(payload)`,
  `apply_override_ended(payload)`: blockierend, `retry_write`, READ-COMMITTED, `replaces` →
  `DELETE WHERE source=? AND kind='window'` + INSERT in einer Transaktion; Upsert je
  `override_id`; gelegentlich `purge_expired`.
- `maintenance_for(ci_ids, now) -> dict[ci_id, {active, until, source, next_start, overrides:
  [...]}]` über `core.api.maintenance.effective_state` (ohne Bibliothek: `active=None`).
- `list_servers`/`get_server` (Service-Seite, nicht `to_dict`): Feld `maintenance` je Record mit
  gesetzter `ci_id` per **einer** Batch-Abfrage.

## 4. Bus-Handler (`entrypoint.py`, Muster `_on_register_server` 281-296)

Drei Handler: `isinstance(payload, dict)`, `await svc.wait_ready()`, `asyncio.to_thread(...)`,
Fehler loggen (nicht `ingest_failed`, dessen Payload ist `source/external_id`-förmig).
`payload["_origin"]` nicht filtern (Idempotenz statt Filter).

## 5. API (`app/api.py`)

- `GET /servers`, `GET /servers/{id}`: Feld `maintenance` (additiv).
- `GET /servers?in_maintenance=true`: Filter.
- `GET /maintenance/{ci_id}`: vollständiges Read-Model (Fenster je Quelle, Overrides) für den
  Tooltip/Later-Detail.

## 6. UI (`src/ui/PluginApp.tsx`, `src/ui/lib/api.ts`, `locales/server_manager.{de,en}.json`)

- `ServerRecord` (`api.ts:6-35`) + `maintenance?`.
- `MaintenanceBadge` neben `SyncIndicator` (106) in der Status-Zelle (338-342): Label
  „Wartung bis hh:mm" bzw. „Wartung ab …" (nächstes Fenster innerhalb 24 h), Tooltip mit Quelle,
  Grund, Ende. Klasse `lx-badge--maintenance` mit Fallback `lx-badge--paused`.
- Filter „In Wartung" bei den Selects (249-262), Logik im `useMemo` (196).
- In der Connection-Zelle (303-337) zusätzlich ein Icon „Registry öffnen" existiert bereits;
  „betroffene Services" nur als Deep-Link in die Registry (`/apps/lyndrix-plugin-entity_registry/registry?uuid=<id>`).
- NiceGUI (`app/ui/overview.py:192-220`): ohne Änderung.
- Bundle `ui_static/ui_bundle.js` neu bauen und committen.

## 7. Tests (`tests/test_maintenance.py`, Fixture aus `tests/test_ingest.py:19-42`)

`replaces` je Quelle; Idempotenz bei doppelter Zustellung; Override Start/Ende/Linger/Ablauf;
Event für `ci_id` ohne Record wird gespeichert und nach `register_server` sichtbar;
`list_servers` liefert `maintenance` nur für Records mit `ci_id`; Filter `in_maintenance`;
Verhalten ohne Core-Bibliothek. SQLite mit `PRAGMA foreign_keys=ON`.

## 8. Abnahme

Dev-Bench: Host mit Fenster aus dem Orchestrator zeigt das Badge; manuelles Override aus dem
Monitoring erscheint. `ruff`, `black --check`, `mypy`. Tags `v1.4.0`/`1.4.0` nach Freigabe.
