# Teilplan P5a: Entity Registry 0.3.0 — Read-Model `maintenance`, Scheme `iac.service`

Ausführung: Fable-Subagent. Grundlage: `analysis-entity-registry.md`, `00-ergebnis-p0.md`
(Abschnitte 1–4). Repo `plugins/lyndrix-plugin-entity-registry` (kanonisches Layout).
**Vor dem Orchestrator ausrollen** (Scheme `iac.service`). Core-Bibliothek optional:
`try: from core.api import maintenance except ImportError: maintenance = None` → ohne
Bibliothek werden nur Rohdaten geliefert (`active: null`).

## 1. Manifest und Katalog

- `entrypoint.py:75` `version="0.3.0"`; `min_core_version` bleibt `1.4.0`.
- `permissions.subscribe` (94-103) + `maintenance:windows_published`,
  `maintenance:override_started`, `maintenance:override_ended`.
- `catalog/entity_types.yml` `identifier_schemes`: neu
  `{id: iac.service, label: IaC service instance, description: "<host>/<service-name> — a
  service.yml deployed on one host", priority: 20}`. Kinds unverändert (`service` existiert).
- CHANGELOG; `documentation.md` (DE): Tabellenübersicht (31), Bus-Tabelle (110ff), neue Route,
  Abschnitt „Wartungsfenster am CI".

## 2. Datenmodell (`app/model/models.py`, DDL über `create_all` unter Advisory-Lock)

```
registry_maintenance  id PK, entity_uuid String(36) FK→registry_entities.uuid idx,
                      kind String(16)  -- window | override
                      source String(64), override_id String(64) NULL,
                      windows JSON NULL, inherited_from String(32) NULL, deploy_policy String(16) NULL,
                      incidents JSON NULL,
                      until DateTime NULL, started_at DateTime NULL, ended_at DateTime NULL,
                      linger_minutes Integer NULL, reason Text NULL, updated_at
                      UNIQUE (entity_uuid, source, kind, override_id)
```

Keine Nutzung von `Entity.attributes` (Begründung analysis §9.1: kein Namespace, RMW-Race,
Event-Rauschen).

## 3. Logik (`app/logic/service.py`, neue Methoden; alle über `self._write` mit Retry)

- `apply_windows(source, items, replaces: bool)`: Transaktion: `replaces` → `DELETE … WHERE
  source=? AND kind='window'`; je Item `_resolve_live(ci_id)` (Tombstones folgen, unbekannt →
  still überspringen, Zähler ins Log) → INSERT (`windows`, `inherited_from`, `deploy_policy`,
  `incidents`). Kind-Feld des Items wird ignoriert (Typ steht an der Entity). **Kein**
  `entity_updated` je Entity; ein Sammel-Event `entity_registry:maintenance_changed
  {source, count}` (neu in `permissions.emit`) für UIs.
- `start_override(payload)`: Upsert je `(entity_uuid, source, 'override', override_id)` für jede
  `ci_id` in `ci_ids`; `end_override(payload)`: `ended_at`, `linger_minutes` setzen (alle
  Zeilen des `override_id`).
- `maintenance_for(s, uuids: list[str], now) -> dict[uuid, dict]`: eine `IN`-Abfrage; je Entity
  `windows` (alle Quellen), Overrides (nur aktive nach `override_active`), Ergebnis
  `{"active": bool|None, "until": iso|None, "source": "window"|"override"|None, "next_start":
  iso|None, "windows": [{source, windows, inherited_from}], "overrides": [{id, until, reason,
  source}], "incidents": {...}|None, "deploy_policy": str|None}` über
  `core.api.maintenance.effective_state`; ohne Bibliothek `active=None` und Rohdaten.
- `purge_expired(now)`: Overrides mit `until + linger < now − 1 Tag` löschen; Aufruf
  gelegentlich aus `end_override`/`start_override` (kein Timer im Plugin).
- `_entity_detail` (156-190): Feld `maintenance`; `_summaries`/`list_entities` (233-236,
  314-320): Feld `maintenance` kompakt (`active`, `until`, `source`) per Batch-Abfrage.

## 4. Bus-Handler (`entrypoint.py`, Muster `_on_relate` 275-293)

Drei `@ctx.subscribe`-Handler: Payload-Prüfung (`isinstance dict`, `items`/`override_id`),
`svc.is_ready`, `asyncio.to_thread`, Fehler über `_fail`. Idempotent (Unique-Key, delete+insert
je `source`). Lokale Zustellung reicht (Zustand in der DB, Plugin auf jeder Instanz).

## 5. API (`app/api/__init__.py`)

| Methode | Pfad | Perm | Inhalt |
|---|---|---|---|
| GET | `/entities/{uuid}` | read | enthält `maintenance` (Abschnitt 3) |
| GET | `/entities?in_maintenance=true` | read | Filter (nach Batch-Berechnung, Seite begrenzt) |
| GET | `/maintenance?ci_ids=a,b,c` (max 200) | read | Batch-Read-Model für Konsumenten/UIs |
| GET | `/entities/{uuid}/host` | read | Host-Entity über ausgehende `runs_on`-Relation, 404 wenn keine |

## 6. UI (`app/ui/react/PluginApp.tsx`, `lib/api.ts`, `locales/entity_registry.{de,en}.json`)

- `MaintenanceBadge` (Klasse `lx-badge--maintenance`, Fallback `lx-badge--paused`, Icon
  `engineering`, Tooltip Quelle/Ende) in der Typ-Zelle der Liste (207) und in der Detail-Kopfzeile
  (285).
- Detail: neue Karte „Wartung" vor der ITSM-Karte (367): aktuelles Fenster (bis), nächstes
  Fenster (ab), Overrides (Grund, Ende, Quelle), Quellen der Fenster mit `inherited_from`,
  Incident-Regel.
- Typ `Entity` (`lib/api.ts:27ff`) + `maintenance?`. Bundle `app/ui/static/ui_bundle.js` neu
  bauen und committen.

## 7. Tests (`tests/test_registry.py` Fixture `svc`, neue Datei `tests/test_maintenance.py`)

- `apply_windows` ersetzt je `source`, lässt fremde Quellen stehen; unbekannte `ci_id`
  übersprungen; Tombstone-UUID landet beim Ziel; doppelte Zustellung idempotent.
- Override Start/Ende/Linger/Ablauf; `maintenance_for` mit und ohne Bibliothek (Monkeypatch
  `maintenance = None`); `effective_state` mit gleichzeitigem Fenster und Override.
- Detail und Liste enthalten `maintenance`; Filter `in_maintenance`; Batch-Route; `/host` über
  `runs_on`; Katalog lädt `iac.service`; `identify` mit `iac.service` funktioniert.
- Bus-Handler mit `_FakeCtx` (bisher ungetestet): ungültiger Payload → `ingest_failed`.

Lauf im Dev-Core (`docker cp` + `PYTHONPATH=/app`), `ruff`, `black --check`.

## 8. Abnahme

Dev-Bench: Orchestrator 1.6.0 publiziert → `GET /entities/<host>` zeigt Fenster; Override aus
dem Monitoring erscheint und verschwindet nach Linger. Tags `v0.3.0`/`0.3.0` nach Freigabe.
