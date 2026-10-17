# Teilplan P3: Monitoring 1.7.0 — `ci_id`, Zustand `MAINTENANCE`, Fenster/Overrides, Outage-Regel

Ausführung: Fable-Subagent. Grundlage: `analysis-monitoring.md`, `00-ergebnis-p0.md`
(Abschnitte 2–4). Repo `plugins/lyndrix-plugin-monitoring`, Layout bleibt `app/controller/`.
Voraussetzung: Core 1.11.0 (API 1.13); Orchestrator 1.6.0 liefert `ci_id`/`host_ci_id` im
Inventory-Sync (bis dahin bleibt `ci_id` leer, alles andere funktioniert).

## 1. Manifest und Versionen

- `entrypoint.py:49` `version="1.6.2" → "1.7.0"`, `min_core_version="1.11.0"`.
- `permissions.subscribe` + `maintenance:windows_published`, `maintenance:override_started`,
  `maintenance:override_ended`. `permissions.emit` + `maintenance:windows_published`,
  `maintenance:override_started`, `maintenance:override_ended`, `monitoring:outage_persisted`,
  `monitoring:outage_recovered`.
- `settings_schema` (71-110) Kategorie „Maintenance" / „Incidents":

| Key | kind | Default |
|---|---|---|
| `outage_rules_enabled` | bool | true (globaler Notschalter) |
| `outage_rules_enabled_hypervisor` / `_host` / `_service` / `_container` | bool | true / true / true / false |
| `outage_default_offline_minutes` | int | 10 |
| `outage_default_severity` | select critical/high/medium/low | high |
| `outage_default_auto_resolve` | bool | true |
| `override_default_minutes` | int | 60 (manuelles Override aus der UI) |
| `override_max_minutes` | int | 240 |
| `override_linger_minutes` | int | 15 |

`apply_settings` (`service.py:994-1010`) übernimmt sie; Änderungen kommen per
`plugin:settings_changed` ohne Reload.

## 2. Datenmodell (`app/model/models.py`, DDL über `create_all` + `_ensure_schema`)

- `MonitorRecord` (26-56): `ci_id String(36) NULL` mit Index; `MonitorUpsert.ci_id`;
  `_serialize_monitor` liefert `ci_id`; `upsert_monitor` (`service.py:1655-1692`) übernimmt es;
  **`config_hash` (1653-1654) schließt `ci_id` ein**; `_ensure_schema` (726-744) `ALTER … ADD
  COLUMN IF NOT EXISTS ci_id`. Inventory-Ingest (`ingest_inventory_snapshot` 2155-2265) liest
  `ci_id` aus `hosts[]`/`services[]`.
- `MonitorState.MAINTENANCE = "MAINTENANCE"` (12-16). `PassiveResult.state` lehnt
  `MAINTENANCE` ab (400).
- `monitoring_daily_aggregates`: Spalte `maintenance_samples Integer default 0`
  (`_ensure_schema`, MySQL-Upsert 1120-1156, `_bump_daily_aggregate` 1073-1110). Uptime-Nenner
  `total - paused - maintenance` in `_uptime_from_aggregates` (`models.py:222-229`),
  `_refresh_uptime_windows_sync` (`service.py:1202`), `_calculate_uptime_percentages`
  (`models.py:201`); `_timeline_uptime_percent` zählt `UP` unverändert.
- Neue Tabellen (alle `extend_existing`, FK-frei):

```
monitoring_maintenance_windows  id PK, ci_id String(36) idx NULL, monitor_id String(191) idx NULL,
                                source String(64), kind String(16), name String(255),
                                windows_json Text, inherited_from String(32) NULL,
                                incidents_json Text NULL, deploy_policy String(16) NULL,
                                note Text NULL, updated_at; UNIQUE (source, ci_id, monitor_id)
monitoring_overrides            override_id String(64) PK, source String(64), ci_ids_json Text,
                                monitor_ids_json Text NULL, until DateTime, started_at DateTime,
                                ended_at DateTime NULL, linger_minutes Integer, reason Text
monitoring_outages              monitor_id String(191) PK, outage_id String(128) NULL, ci_id String(36) NULL,
                                down_since DateTime, suppressed_until DateTime NULL,
                                state String(16)  -- open | persisted | recovered
                                persisted_at DateTime NULL, recovered_at DateTime NULL,
                                rule_json Text NULL, error Text NULL, updated_at
```

Zeiten naiv UTC wie der Rest des Plugins (`models.py:251`); Konvertierung aus/zu aware über
Helfer in `maintenance.py`.

## 3. Modul `app/controller/maintenance.py` (neu)

- `MaintenanceStore`: CRUD mit `_rc_session` + `_retrying` (1020/1205/1213):
  `replace_windows(source, items)` (DELETE je `source` + INSERT in einer Transaktion),
  `add_manual_window(ci_id|monitor_id, windows, note)`, `delete_window(id)`,
  `upsert_override(...)`, `end_override(override_id, ended_at, linger)`, `list_*`,
  `purge_expired(now)` (Overrides mit `until + linger < now - 1 d`), `open_outages()`.
- `MaintenanceCache` (je Instanz, In-Memory, kein DB-Lesen im Probe-Hot-Path):
  `reload()` lädt Fenster (`parse_windows`), Overrides (`Override`), Regeln je `ci_id`, und die
  Zuordnung `monitor_id → ci_id` (Monitor-Spalte, Fallback: Host-Monitor `host:<host_name>`
  für Services ohne eigene `ci_id`); `state_for(record, now) -> MaintenanceState` über
  `core.api.maintenance.effective_state`; `rule_for(record) -> IncidentRule | None`
  (Reihenfolge: Regel am Service-CI → Regel am Host-CI → Settings-Default des Kinds;
  Kind aus `windows`-Tabelle, sonst `server`-Monitor → `host`, sonst `service`); Notschalter
  (`outage_rules_enabled*`) hier anwenden.
- Bus-Handler (dünn in `entrypoint.py` nach Muster 327-367, Arbeit im Modul via `to_thread`):
  `on_windows_published` (nur `source != "monitoring"`; `replaces` → `replace_windows`),
  `on_override_started`, `on_override_ended`. Nach jedem Schreiben: `cache.reload()` lokal und
  `_emit_cluster("monitoring:monitors_changed", reason="maintenance")`; `on_monitors_changed`
  (`service.py:436-446`) bekommt den Zweig `reason == "maintenance" → cache.reload()` (kein
  Resync). Handler sind idempotent (Unique-Keys, Upsert je `override_id`).
- Manuelle Fenster/Overrides aus der UI: schreiben, dann **lokal** `ctx.emit` von
  `maintenance:windows_published {source:"monitoring", replaces:true, items}` bzw.
  `override_started/ended {source:"monitoring"}`, damit Registry und Server Manager sie sehen;
  die eigenen Handler ignorieren `source == "monitoring"` (sonst Doppelschreiben).

## 4. Unterdrückung im Schreibpfad (`service.py:1478-1506 _store_heartbeat`)

Alle Heartbeat-Pfade (aktiv 643, passiv 1749) laufen hier durch. Reihenfolge:

1. `ms = cache.state_for(record, now)`; `effective = MAINTENANCE if state == DOWN and ms.active
   else state`. `UP` bleibt `UP`. `latest_error` bleibt zur Diagnose erhalten.
2. Heartbeat mit `effective`, `_bump_daily_aggregate` zählt `maintenance_samples`.
3. **Outage-Buchführung in derselben Session** (konsistent mit dem Commit, keine Phantome):
   - `effective in (DOWN, MAINTENANCE)` und keine Zeile → `INSERT monitoring_outages(state="open",
     down_since=now, suppressed_until = ms.until if ms.active else NULL)`.
   - vorhandene offene Zeile und `ms.active` → `suppressed_until = max(suppressed_until, ms.until)`.
   - `effective == UP` und Zeile vorhanden: `state == "open"` → Zeile löschen (nie gemeldet);
     `state == "persisted"` → `state="recovered"`, `recovered_at=now`, Event
     `outage_recovered` **nach dem Commit** emittieren (Ereignisliste an der Session, Emit im
     Aufrufer nach `commit()`), Zeile danach löschen.
   - `apply_admin_override(pause)` (1703) und `delete_monitors`/`clear_states_db` (1929, 1803)
     räumen `monitoring_outages` mit.
4. `_emit_transition` (1462-1476): Payload um `ci_id` und `maintenance: bool` ergänzen; weiter
   vor dem Commit (bestehendes Verhalten, nur für Cache/UI).

Flap-Dämpfung (2128-2134) bleibt vor diesem Punkt; der Wartungs-Check findet erst hier statt.

## 5. Outage-Auswertung (Interval-Job `monitoring:outages`, 60 s)

- Registriert in `start()` (184-200), läuft nur auf der probenden Instanz; im Shard-Modus für
  `owns(monitor_id)` (383) und zusätzlich der `housekeeping`-Worker für fremde Reste nach
  Shard-Wechsel.
- Je offener Zeile (`state == "open"`): `rule = cache.rule_for(record)`; `None`/disabled →
  weiter. `ms = cache.state_for(record, now)`; aktiv → `suppressed_until = max(…, ms.until)`,
  weiter. Sonst `effective_since = max(down_since, suppressed_until or down_since)`;
  `now - effective_since >= offline_minutes` → bedingtes `UPDATE … SET state='persisted',
  persisted_at=now, outage_id=make_outage_id(ci_id, down_since), rule_json=… WHERE
  monitor_id=? AND state='open'`; `rowcount == 1` → **einmal** `ctx.emit(TOPIC_OUTAGE_PERSISTED,
  {...})` (lokal). Ohne `ci_id` am Monitor (auch nicht über den Host-Monitor): kein Event, Log
  einmal je Monitor.
- Payload: `outage_id, ci_id, kind, name (Monitor-Name), host_ci_id (ci_id des Host-Monitors,
  bei Host-Monitoren leer), monitor_id, since, persisted_at, duration_s, rule {offline_minutes,
  severity, auto_resolve}, error (latest_error)`.
- Recovery-Event siehe Abschnitt 4; `duration_s = recovered_at - down_since`.

## 6. Zustand im Dashboard

- `aggregate_state` (`app/ui/styles.py:78-90`) und React-Äquivalent: `DOWN > UNKNOWN >
  MAINTENANCE > UP > PAUSED`.
- `status_counts`/`stats` (1232-1284): `maintenance` zählen; `health()` unverändert.
- `_serialize_monitor` (`models.py:162`), `list_monitors` (1286), `helpers.py:147,271-307`
  (Whitelist-Dicts, Host-/Service-Views 180-200): Felder `ci_id`, `in_maintenance`,
  `maintenance_until`, `maintenance_source`, `outage_since` (bei offener/persistierter Zeile).
  Overview-Cache (TTL 10 s) wird bei Fensteränderung geleert (`_view_cache.clear()`).
- `_sync_job_on_loop` (1550): `MAINTENANCE` behält den Job (Probing läuft weiter, Recovery
  wird erkannt). `_load_probe_candidates` (894-903): `MAINTENANCE` wie `DOWN` behandeln.

## 7. API (`app/controller/api.py`, R/W wie bisher)

| Methode | Pfad | Perm | Inhalt |
|---|---|---|---|
| GET | `/windows?ci_id=&monitor_id=&source=` | R | Fenster inkl. `effective_state` je Zeile |
| POST | `/windows` | W | manuelles Fenster `{ci_id \| monitor_id, windows: [Eingabe- oder Normalform], note}`; Validierung über `parse_windows` (400 mit Meldungen) |
| DELETE | `/windows/{id}` | W | nur `source == "monitoring"` |
| GET | `/overrides?active=true` | R | Overrides mit `active`, `until_effective` |
| POST | `/overrides` | W | `{ci_ids \| monitor_ids, minutes (<= override_max_minutes), reason}` → `override_id = "mon-<uuid>"` |
| DELETE | `/overrides/{id}` | W | beendet (`ended_at=now`, linger) — nur eigene (`source == "monitoring"`) |
| GET | `/outages` | R | offene/persistierte Ausfälle mit Regel und effektiver Dauer |

Pfade `/maintenance/*` bleiben der Plugin-Pflege; `/settings` ist reserviert.

## 8. UI (`src/ui/PluginApp.tsx`, `src/ui/lib/api.ts`, `locales/monitoring.{de,en}.json`)

- `MonitorState` (12) + `MAINTENANCE`; `STATE_COLOR` (140-149)
  `var(--lx-state-maintenance, var(--lx-state-paused))`; `STATE_LABEL`/`StateBadge` (320-353)
  mit Zusatz „bis hh:mm" aus `maintenance_until`; `HeaderCard`-Zähler (1053-1059) „Wartung".
- Typen `ServiceView`, `Host`, `Row` (14-66): `ci_id?`, `in_maintenance?`, `maintenance_until?`,
  `outage_since?`.
- Neue Komponenten (eigene Dateien unter `src/ui/`): `MaintenanceDialog` (Override anlegen:
  Dauer-Preset 30/60/120/240 min, Grund; optional wiederkehrendes Fenster weekly/once) und
  `OverridesPanel` (aktive Overrides mit „Beenden"). Einhängen: Button in `HostCardDetail`
  (519), `SplitView` (771), Toolbar (873) für Mehrfachauswahl, Panel neben `HeaderCard`.
- NiceGUI (`app/ui/styles.py:8-27`, `widget.py:25`, `_components/host_card.py`): Farbe und
  Label für `MAINTENANCE`; kein Dialog (React ist Ziel).

## 9. Cluster

- Prober-Lease unverändert; der Outage-Job hängt an `start()`/`stop()` wie die anderen
  Housekeeping-Jobs. Standby-Instanzen halten den Cache für API-Antworten aktuell (Reload über
  `monitors_changed reason=maintenance`), werten aber nicht aus.
- DB-Writes der Handler mit `_rc_session` + `_retrying`; `replace_windows` in einer kurzen
  Transaktion; IntegrityError = „Peer war schneller".
- `_FakeCtx.emit` in den Tests bekommt `cluster=False`, damit der `cluster=True`-Pfad getestet
  wird (heute über den `TypeError`-Zweig 485-488 verdeckt).

## 10. Tests (`tests/`, Rezept in `analysis-monitoring.md` §7)

- `test_maintenance.py` (bestehende Datei behandelt Plugin-Pflege → neue Datei
  `test_maintenance_windows.py`): Fenster ersetzen je `source`; Override-Start/Ende/Linger/Ablauf;
  Cache-Reload bei `monitors_changed reason=maintenance`; manuelle Fenster emittieren mit
  `source=monitoring` und werden vom eigenen Handler ignoriert; Validierungsfehler 400.
- `test_store_heartbeat_maintenance.py`: DOWN im Fenster → `MAINTENANCE` gespeichert, Aggregat
  `maintenance_samples`, Uptime-Nenner; UP im Fenster bleibt UP; Outage-Zeile wird angelegt,
  `suppressed_until` fortgeschrieben, bei UP gelöscht; passive Ergebnisse gleich behandelt;
  `PassiveResult` mit `MAINTENANCE` → 400.
- `test_outage_rule.py` mit Fake-Uhr (`monkeypatch` auf `models._utc_now` und die
  Maintenance-Uhr): persistiert nach `offline_minutes` außerhalb Fenster; nicht innerhalb;
  Uhr startet nach Fensterende neu; Regelkaskade Service-CI → Host-CI → Settings; Notschalter
  je Kind; ohne `ci_id` kein Event; doppelte Auswertung liefert genau einen Emit (bedingtes
  UPDATE); Recovery emittiert nach Commit mit `duration_s`; Payload-Felder vollständig.
- `test_inventory_ci_id.py`: Ingest übernimmt `ci_id`/`host_ci_id`; Änderung der `ci_id`
  ändert `config_hash`.

## 11. Doku und Abnahme

- `documentation.md` (DE): Abschnitt „Events" (ab 185) neu schreiben (Ist-Zustand plus neue
  Topics), neuer Abschnitt „Wartungsfenster, Overrides und Outage-Regel", Settings-Tabelle;
  `docs/index.md:8` korrigieren; `.claude/skills/run-state-monitoring/SKILL.md` React-Route
  richtigstellen; CHANGELOG 1.7.0.
- Abnahme: `00-ergebnis-p0.md` Abschnitt 7, Schritte 3–6. `ruff`, `black --check`, `mypy`;
  Bundle neu gebaut; Commit auf `feature/maintenance-windows`; Tags `v1.7.0`/`1.7.0` nach
  Freigabe.
