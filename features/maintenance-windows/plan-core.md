# Teilplan P1: Core-Bibliothek `core.api.maintenance` (API 1.13, Core 1.11.0)

Ausführung: Fable selbst. Grundlage: `00-ergebnis-p0.md` Abschnitte 2, 3. Der Core bleibt
plugin-agnostisch: reine Funktionen, Konstanten, Doku, ein Farbtoken. Keine Persistenz, keine
Endpunkte.

## 1. Dateien

| Datei | Änderung |
|---|---|
| `lyndrix-core/app/core/api/maintenance.py` | neu, die Bibliothek |
| `lyndrix-core/app/core/api/__init__.py` | `__api_version__ = "1.13.0"`, Docstring-Kopf „v1.13", `from . import maintenance` + `"maintenance"` in `__all__` (Präzedenz: `core.api.route_order` wird als Submodul importiert) |
| `lyndrix-core/app/version.py` | `1.11.0`, `__release_date__`, Codename |
| `lyndrix-core/app/core/theming/schema.py:42` | `--lx-state-maintenance` in die Token-Whitelist |
| `lyndrix-core/app/ui/theme.py` (Zeilen um 148 und 608) | CSS-Variable + Badge-Klasse `lx-badge--maintenance` für NiceGUI |
| `lyndrix-core/app/assets/themes/*/tokens.json` | Token `state_maintenance` (light/dark) in jedem mitgelieferten Theme; Validator füllt fehlende Keys auf, trotzdem in `default` setzen (Vorschlag `#a78bfa` dunkel, `#7c3aed` hell) |
| `lyndrix-ui/src/index.css:58-61` und `:453,460` | `--lx-state-maintenance: #a78bfa;` plus `.lx-badge--maintenance`; `package.json` 1.8.0 |
| `lyndrix-core/docs/plugins.md` | neuer Abschnitt „## Maintenance (API 1.13)" nach „AI instructions" |
| `lyndrix-core/tests/maintenance/test_schedule.py`, `test_contract.py` | neu |
| `lyndrix-dev/CLAUDE.md` | Abschnitt „### Maintenance (core API ≥ 1.13)" (äußeres Repo, Marvins Commit) |

## 2. Öffentliche API der Bibliothek

```python
# Konstanten
KNOWN_KINDS = ("hypervisor", "host", "service", "container", "network", "itsm_record", "generic")
    # nur Doku/UI-Reihenfolge — KEIN Gate (Entscheidung A: Kinds sind offen)
def is_valid_kind(kind: str) -> bool        # ^[a-z][a-z0-9_]*$, max 32 Zeichen
SEVERITIES = ("critical", "high", "medium", "low")
DEPLOY_POLICIES = ("window", "immediate")
TOPIC_WINDOWS_PUBLISHED = "maintenance:windows_published"
TOPIC_OVERRIDE_STARTED = "maintenance:override_started"
TOPIC_OVERRIDE_ENDED = "maintenance:override_ended"
TOPIC_WINDOW_MISSING = "maintenance:window_missing"
TOPIC_OUTAGE_PERSISTED = "monitoring:outage_persisted"
TOPIC_OUTAGE_RECOVERED = "monitoring:outage_recovered"
DEFAULT_LINGER_MINUTES = 15
DEFAULT_MAX_OVERRIDE_MINUTES = 240
DEFAULT_HORIZON = timedelta(days=30)

class ScheduleError(ValueError): ...

@dataclass(frozen=True) class WeeklySchedule:  days: frozenset[int]  # 0 = Montag … 6 = Sonntag (ISO)
                                               start: time; end: time; tz: str
@dataclass(frozen=True) class OnceSchedule:    start: datetime; end: datetime; note: str | None   # aware
@dataclass(frozen=True) class CronSchedule:    expr: str; duration_minutes: int; tz: str
Schedule = WeeklySchedule | OnceSchedule | CronSchedule

def parse_window(raw: Mapping, *, default_tz: str = "UTC") -> Schedule
    # akzeptiert Eingabeform {"weekly": {...}} / {"once": {...}} / {"cron": {...}}
    # und Normalform {"type": "weekly", ...}; wirft ScheduleError mit sprechender Meldung
def parse_windows(raw: Sequence[Mapping] | None, *, default_tz="UTC") -> list[Schedule]
def validate_windows(raw, *, default_tz="UTC") -> list[str]        # Fehlertexte statt Exception (Linter, UI)
def to_dict(s: Schedule) -> dict                                     # Normalform (Abschnitt 2.1 im Ergebnis)
def to_dicts(schedules) -> list[dict]

class Interval(NamedTuple): start: datetime; end: datetime         # aware, UTC
def intervals_between(schedules, start, end, *, limit=500) -> list[Interval]   # sortiert, überlappende zusammengeführt
def current_interval(schedules, now) -> Interval | None             # enthält now (start <= now < end), größtes end
def active_until(schedules, now) -> datetime | None
def next_window(schedules, after, *, horizon=DEFAULT_HORIZON) -> Interval | None
    # frühestes Intervall mit end > after; liegt after darin, ist es das laufende (start <= after)
def next_start(schedules, after, *, horizon=DEFAULT_HORIZON) -> datetime | None   # Start des nächsten noch nicht laufenden Intervalls

@dataclass class Override:
    override_id: str; ci_ids: tuple[str, ...]; until: datetime; started_at: datetime
    ended_at: datetime | None = None; linger_minutes: int = DEFAULT_LINGER_MINUTES
    reason: str = ""; source: str = ""
def override_until(o: Override) -> datetime     # ended_at + linger, sonst until
def override_active(o: Override, now) -> bool
def override_from_payload(payload: Mapping) -> Override     # aus override_started (+ ended-Merge separat)

@dataclass(frozen=True) class MaintenanceState:
    active: bool; until: datetime | None; source: Literal["window", "override"] | None
    next_start: datetime | None; note: str | None
def effective_state(schedules, overrides, now, *, horizon=DEFAULT_HORIZON) -> MaintenanceState
    # Override gewinnt bei gleichzeitiger Aktivität (längeres until), source="override"

@dataclass(frozen=True) class IncidentRule:
    enabled: bool; offline_minutes: int; severity: str; auto_resolve: bool
def parse_incident_rule(raw: Mapping | None, *, fallback: IncidentRule | None = None) -> IncidentRule | None
    # offline_minutes 0 → enabled False; severity validiert; unbekannte Keys ignoriert
def resolve_incident_rule(own, type_default, site_default) -> IncidentRule | None   # erste definierte Ebene gewinnt
def rule_to_dict(rule) -> dict

def parse_iso(value: str) -> datetime        # aware; naive → UTC
def iso(dt: datetime) -> str                 # UTC, Sekunden, "Z"
def utcnow() -> datetime
def make_outage_id(ci_id: str, since: datetime) -> str      # f"{ci_id}:{iso(since)}"
def make_window_missing_id(ci_id: str, job_ref: str | int) -> str   # f"window_missing:{ci_id}:{job_ref}"
```

Payload-Formen aus `00-ergebnis-p0.md` Abschnitt 3 als `TypedDict`s (`WindowsPublishedItem`,
`WindowsPublished`, `OverrideStarted`, `OverrideEnded`, `WindowMissing`, `OutagePersisted`,
`OutageRecovered`) nur zur Dokumentation und für Typprüfung; keine Laufzeitvalidierung im Core.
`parse_incident_rule`/`resolve_incident_rule` arbeiten mit `incident_defaults` als
`Mapping[str, Mapping]` mit **beliebigen** Kind-Schlüsseln (`resolve_incident_rule(own,
defaults_by_kind, kind, site_defaults_by_kind)`).

## 3. Zeitlogik, präzise

- **Weekly:** Für jeden Kalendertag `d` im Fenster `[after − 1 Tag, after + horizon]` (lokal in
  `tz`): Tag in `days` → lokaler Start `d + start`, Ende `d + end`, bei `end <= start` Ende am
  Folgetag. Lokale Zeiten über `zoneinfo.ZoneInfo(tz)` nach UTC; nicht existierende Zeiten
  (Frühjahrsumstellung) werden von `zoneinfo` vorwärts normalisiert, doppelte Zeiten
  (Herbst) mit `fold=0`. Horizon begrenzt die Iteration.
- **Once:** genau ein Intervall; `start < end` Pflicht; `tz`-Offset Pflicht (oder `default_tz`
  bei naiven Werten).
- **Cron:** eigener Parser `CronExpr` (5 Felder: Minute, Stunde, Tag, Monat, Wochentag).
  `*`, `a`, `a-b`, `a,b`, `*/n`, `a-b/n`, Monatsnamen `jan…dec`, Wochentagsnamen `sun…sat`,
  `0` und `7` = Sonntag. Vixie-Semantik: sind Tag **und** Wochentag eingeschränkt, zählt
  „einer von beiden". `next_fire(after_local)` sucht minutenweise mit Sprüngen über Felder
  (Monat → Tag → Stunde → Minute), Obergrenze 5 Jahre, sonst `None`. Intervall =
  `[fire, fire + duration]` in `tz`, nach UTC konvertiert. Für `current_interval` muss auch ein
  Fire vor `now` geprüft werden: `last_fire(before)` rückwärts bis `duration` zurück.
- Alle Rückgaben sind **aware UTC**. Eingaben ohne Zeitzone gelten als UTC (`parse_iso`).
- `intervals_between` vereinigt überlappende Intervalle aller Schedules (Fenster addieren sich).

## 4. Tests (`tests/maintenance/`)

Laufen im Dev-Core (`docker exec lyndrix-core-dev python -m pytest tests/maintenance -q`).

- Parsing: jede Eingabeform und die Normalform; Fehlerfälle (unbekannter Typ, `HH:MM` falsch,
  `days` leer, `once.end <= start`, Cron 4 Felder, `duration_minutes 0`, unbekannte `tz`) mit
  Prüfung des Meldungstexts; `to_dict(parse_window(x))` ist idempotent.
- Weekly: Sonntag 02:00–04:00 Berlin → UTC-Intervalle im Winter (01:00–03:00Z) und Sommer
  (00:00–02:00Z); Fenster über Mitternacht (`22:00–02:00`); Umstellungstage 2026-03-29 und
  2026-10-25 (Fenster 02:00–03:00 am Umstellungstag: Frühjahr normalisiert, Herbst 1 h länger
  erlaubt, Test dokumentiert das Verhalten); `days` als Zahlen und Namen.
- Cron: `0 2 1 * *` + 180 min über Monats- und Jahreswechsel; `*/15 * * * *`; `0 3 * * sun`;
  Vixie-OR `0 0 13 * fri`; `last_fire` für `current_interval`; `None` nach Horizon.
- `next_window`/`next_start`: vor, in und nach einem Intervall; leere Liste; Horizon 30 Tage.
- `effective_state`: nur Fenster, nur Override, beides (Override gewinnt), Override nach
  `ended_at` innerhalb und außerhalb `linger`, abgelaufen.
- Incident-Regeln: `offline_minutes: 0` → disabled; Severity-Validierung; Kaskade
  `resolve_incident_rule`.
- Contract: Konstanten unverändert (Snapshot-Test), `make_outage_id`/`make_window_missing_id`
  deterministisch; `is_valid_kind` akzeptiert `network`/`my_kind2`, lehnt `VM`/`a-b`/leer ab.

## 5. Doku (`docs/plugins.md`, Englisch wie der Rest der Datei)

Inhalt: Zweck (Core liefert Modell + Zeitauswertung + Topic-Namen, keine Persistenz), Normalform
der Fenster, die sechs Topics mit Payload-Tabelle, Transportregel (lokal emittieren, idempotent
konsumieren, eigener Cluster-Broadcast für Caches), Override-Semantik mit `linger_minutes`,
Typ-Vokabular = Entity-Registry-Kinds (offen, kein Gate; `KNOWN_KINDS` nur Anzeige-Reihenfolge),
`window_missing` → ITSM-Task, Beispiel „Plugin konsumiert `windows_published` und
berechnet `effective_state` beim Lesen", Hinweis auf `min_core_version="1.11.0"` bzw.
`try/except ImportError` für optionale Nutzung.

## 6. Abnahme

- Tests grün im Dev-Core; `ruff`, `black --check`, `mypy app/core/api/maintenance.py` sauber.
- `python -c "from core.api import maintenance as m; print(m.KNOWN_KINDS)"` im Dev-Core.
- Theme-Validator akzeptiert `state_maintenance`; Shell zeigt das Token in den DevTools.
- Commit auf `feature/maintenance-core`, kein Push vor Freigabe; danach Core-Image 1.11.0 und
  Shell 1.8.0 nach dem gewohnten Rollout (Marvin rollt k3s).
