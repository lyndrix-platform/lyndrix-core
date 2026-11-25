# Sub-plan P1: core library `core.api.maintenance` (API 1.13, Core 1.11.0)

**Status 2026-10-18: implemented** (lyndrix-core `50d94f5`, lyndrix-ui `fca418d`, local, not pushed).
Deviations from the draft below; the real file `app/core/api/maintenance.py` is authoritative:
`KINDS` → `KNOWN_KINDS` + `is_valid_kind()` (decision A); additionally `MaintenanceError` as
base of `ScheduleError`/`RuleError`, `CronExpr` public, `UTC`, `apply_override_ended(o,
payload)`, `make_window_missing_id`, `TOPIC_WINDOW_MISSING`, TypedDict `WindowMissing`;
`resolve_incident_rule(own, kind, *defaults_by_kind)` (own block wins and may be partial,
then the first `incident_defaults` map that knows the kind). `weekly.start == end` is an
error. The horizon of `next_start` counts from the end of the running window. 64 tests
(`tests/maintenance/`, also run without the core stack on the host). Codename 1.11.0 "Quiet Hours".

Execution: Fable itself. Basis: `00-ergebnis-p0.md` sections 2, 3. The core stays
plugin-agnostic: pure functions, constants, docs, one color token. No persistence, no
endpoints.

## 1. Files

| File | Change |
|---|---|
| `lyndrix-core/app/core/api/maintenance.py` | new, the library |
| `lyndrix-core/app/core/api/__init__.py` | `__api_version__ = "1.13.0"`, docstring header "v1.13", `from . import maintenance` + `"maintenance"` in `__all__` (precedent: `core.api.route_order` is imported as a submodule) |
| `lyndrix-core/app/version.py` | `1.11.0`, `__release_date__`, codename |
| `lyndrix-core/app/core/theming/schema.py:42` | `--lx-state-maintenance` in the token whitelist |
| `lyndrix-core/app/ui/theme.py` (lines around 148 and 608) | CSS variable + badge class `lx-badge--maintenance` for NiceGUI |
| `lyndrix-core/app/assets/themes/*/tokens.json` | token `state_maintenance` (light/dark) in every bundled theme; validator fills missing keys, still set it in `default` (suggestion `#a78bfa` dark, `#7c3aed` light) |
| `lyndrix-ui/src/index.css:58-61` and `:453,460` | `--lx-state-maintenance: #a78bfa;` plus `.lx-badge--maintenance`; `package.json` 1.8.0 |
| `lyndrix-core/docs/plugins.md` | new section "## Maintenance (API 1.13)" after "AI instructions" |
| `lyndrix-core/tests/maintenance/test_schedule.py`, `test_contract.py` | new |
| `lyndrix-dev/CLAUDE.md` | section "### Maintenance (core API ≥ 1.13)" (outer repo, Marvin's commit) |

## 2. Public API of the library

```python
# Constants
KNOWN_KINDS = ("hypervisor", "host", "service", "container", "network", "itsm_record", "generic")
    # documentation/UI order only — NOT a gate (decision A: kinds are open)
def is_valid_kind(kind: str) -> bool        # ^[a-z][a-z0-9_]*$, max 32 characters
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

@dataclass(frozen=True) class WeeklySchedule:  days: frozenset[int]  # 0 = Monday … 6 = Sunday (ISO)
                                               start: time; end: time; tz: str
@dataclass(frozen=True) class OnceSchedule:    start: datetime; end: datetime; note: str | None   # aware
@dataclass(frozen=True) class CronSchedule:    expr: str; duration_minutes: int; tz: str
Schedule = WeeklySchedule | OnceSchedule | CronSchedule

def parse_window(raw: Mapping, *, default_tz: str = "UTC") -> Schedule
    # accepts input form {"weekly": {...}} / {"once": {...}} / {"cron": {...}}
    # and normal form {"type": "weekly", ...}; raises ScheduleError with a descriptive message
def parse_windows(raw: Sequence[Mapping] | None, *, default_tz="UTC") -> list[Schedule]
def validate_windows(raw, *, default_tz="UTC") -> list[str]        # error texts instead of exception (linter, UI)
def to_dict(s: Schedule) -> dict                                     # normal form (section 2.1 in the result)
def to_dicts(schedules) -> list[dict]

class Interval(NamedTuple): start: datetime; end: datetime         # aware, UTC
def intervals_between(schedules, start, end, *, limit=500) -> list[Interval]   # sorted, overlapping ones merged
def current_interval(schedules, now) -> Interval | None             # contains now (start <= now < end), largest end
def active_until(schedules, now) -> datetime | None
def next_window(schedules, after, *, horizon=DEFAULT_HORIZON) -> Interval | None
    # earliest interval with end > after; if after lies within it, it is the running one (start <= after)
def next_start(schedules, after, *, horizon=DEFAULT_HORIZON) -> datetime | None   # start of the next not-yet-running interval

@dataclass class Override:
    override_id: str; ci_ids: tuple[str, ...]; until: datetime; started_at: datetime
    ended_at: datetime | None = None; linger_minutes: int = DEFAULT_LINGER_MINUTES
    reason: str = ""; source: str = ""
def override_until(o: Override) -> datetime     # ended_at + linger, otherwise until
def override_active(o: Override, now) -> bool
def override_from_payload(payload: Mapping) -> Override     # from override_started (+ ended merge separately)

@dataclass(frozen=True) class MaintenanceState:
    active: bool; until: datetime | None; source: Literal["window", "override"] | None
    next_start: datetime | None; note: str | None
def effective_state(schedules, overrides, now, *, horizon=DEFAULT_HORIZON) -> MaintenanceState
    # override wins on simultaneous activity (longer until), source="override"

@dataclass(frozen=True) class IncidentRule:
    enabled: bool; offline_minutes: int; severity: str; auto_resolve: bool
def parse_incident_rule(raw: Mapping | None, *, fallback: IncidentRule | None = None) -> IncidentRule | None
    # offline_minutes 0 → enabled False; severity validated; unknown keys ignored
def resolve_incident_rule(own, type_default, site_default) -> IncidentRule | None   # first defined level wins
def rule_to_dict(rule) -> dict

def parse_iso(value: str) -> datetime        # aware; naive → UTC
def iso(dt: datetime) -> str                 # UTC, seconds, "Z"
def utcnow() -> datetime
def make_outage_id(ci_id: str, since: datetime) -> str      # f"{ci_id}:{iso(since)}"
def make_window_missing_id(ci_id: str, job_ref: str | int) -> str   # f"window_missing:{ci_id}:{job_ref}"
```

Payload shapes from `00-ergebnis-p0.md` section 3 as `TypedDict`s (`WindowsPublishedItem`,
`WindowsPublished`, `OverrideStarted`, `OverrideEnded`, `WindowMissing`, `OutagePersisted`,
`OutageRecovered`) only for documentation and type checking; no runtime validation in the core.
`parse_incident_rule`/`resolve_incident_rule` work with `incident_defaults` as
`Mapping[str, Mapping]` with **arbitrary** kind keys (`resolve_incident_rule(own,
defaults_by_kind, kind, site_defaults_by_kind)`).

## 3. Time logic, precisely

- **Weekly:** for every calendar day `d` in the window `[after − 1 day, after + horizon]` (local in
  `tz`): day in `days` → local start `d + start`, end `d + end`; if `end <= start`, end on the following day. Local times via `zoneinfo.ZoneInfo(tz)` to UTC; non-existent times
  (spring change) are normalized forward by `zoneinfo`, duplicate times (autumn) with `fold=0`. Horizon limits the iteration.
- **Once:** exactly one interval; `start < end` mandatory; `tz` offset mandatory (or `default_tz`
  for naive values).
- **Cron:** own parser `CronExpr` (5 fields: minute, hour, day, month, weekday).
  `*`, `a`, `a-b`, `a,b`, `*/n`, `a-b/n`, month names `jan…dec`, weekday names `sun…sat`,
  `0` and `7` = Sunday. Vixie semantics: if day **and** weekday are restricted, "one of the two" counts.
  `next_fire(after_local)` searches minute-wise with jumps over fields
  (month → day → hour → minute), upper bound 5 years, otherwise `None`. Interval =
  `[fire, fire + duration]` in `tz`, converted to UTC. For `current_interval` a fire before `now`
  must also be checked: `last_fire(before)` backwards up to `duration`.
- All return values are **aware UTC**. Inputs without time zone count as UTC (`parse_iso`).
- `intervals_between` merges overlapping intervals of all schedules (windows add up).

## 4. Tests (`tests/maintenance/`)

Run in the dev core (`docker exec lyndrix-core-dev python -m pytest tests/maintenance -q`).

- Parsing: every input form and the normal form; error cases (unknown type, `HH:MM` wrong,
  `days` empty, `once.end <= start`, cron with 4 fields, `duration_minutes 0`, unknown `tz`) with
  checking of the message text; `to_dict(parse_window(x))` is idempotent.
- Weekly: Sunday 02:00–04:00 Berlin → UTC intervals in winter (01:00–03:00Z) and summer
  (00:00–02:00Z); window across midnight (`22:00–02:00`); change days 2026-03-29 and
  2026-10-25 (window 02:00–03:00 on the change day: spring normalized, autumn 1 h longer
  allowed, test documents the behavior); `days` as numbers and names.
- Cron: `0 2 1 * *` + 180 min across month and year change; `*/15 * * * *`; `0 3 * * sun`;
  Vixie OR `0 0 13 * fri`; `last_fire` for `current_interval`; `None` after horizon.
- `next_window`/`next_start`: before, inside and after an interval; empty list; horizon 30 days.
- `effective_state`: only window, only override, both (override wins), override after
  `ended_at` inside and outside `linger`, expired.
- Incident rules: `offline_minutes: 0` → disabled; severity validation; cascade
  `resolve_incident_rule`.
- Contract: constants unchanged (snapshot test), `make_outage_id`/`make_window_missing_id`
  deterministic; `is_valid_kind` accepts `network`/`my_kind2`, rejects `VM`/`a-b`/empty.

## 5. Docs (`docs/plugins.md`, English like the rest of the file)

Content: purpose (core delivers model + time evaluation + topic names, no persistence), normal form
of the windows, the six topics with payload table, transport rule (emit locally, consume idempotently, own cluster broadcast for caches), override semantics with `linger_minutes`,
type vocabulary = entity registry kinds (open, no gate; `KNOWN_KINDS` only display order),
`window_missing` → ITSM task, example "plugin consumes `windows_published` and
computes `effective_state` on read", note on `min_core_version="1.11.0"` or
`try/except ImportError` for optional use.

## 6. Acceptance

- Tests green in the dev core; `ruff`, `black --check`, `mypy app/core/api/maintenance.py` clean.
- `python -c "from core.api import maintenance as m; print(m.KNOWN_KINDS)"` in the dev core.
- Theme validator accepts `state_maintenance`; shell shows the token in the DevTools.
- Commit on `feature/maintenance-core`, no push before approval; afterwards core image 1.11.0 and
  shell 1.8.0 after the usual rollout (Marvin rolls out k3s).
