"""Maintenance windows — shared model, schedule evaluation and bus contract (API 1.13).

The core owns the *vocabulary* of the maintenance-window feature so that every
plugin speaks the same language without depending on another plugin:

* the normalised window formats (``weekly``, ``once``, ``cron`` + duration),
* the time evaluation (current/next window in a time zone, DST-safe),
* the override semantics (``until`` + ``linger_minutes``),
* the incident-rule cascade,
* and the topic names + payload shapes of the bus contract.

It holds **no persistence and no endpoints**: plugins store what they receive
in their own tables and evaluate ``effective_state`` when they read. A plugin
that must still load on an older core imports it defensively::

    try:
        from core.api import maintenance
    except ImportError:          # core < 1.11
        maintenance = None

Transport rule (see docs/plugins.md → "Maintenance"): ``maintenance:*`` and
``monitoring:outage_*`` are emitted **locally** (no ``cluster=True``); consumers
persist idempotently in the shared DB and use their own cluster broadcast for
cache invalidation.

Only the standard library is used (``zoneinfo`` for time zones, an own
five-field cron parser) — no new dependency for the core or the plugins.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import (
    Iterable,
    Iterator,
    List,
    Literal,
    Mapping,
    NamedTuple,
    NotRequired,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    Union,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --------------------------------------------------------------------------- #
# Constants — the contract. Changing a value here is an API change.
# --------------------------------------------------------------------------- #

#: Entity-Registry kinds known today. **Documentation and display order only** —
#: kinds are open (a new registry kind needs no core release); validate with
#: :func:`is_valid_kind`, never against this tuple.
KNOWN_KINDS: Tuple[str, ...] = (
    "hypervisor",
    "host",
    "service",
    "container",
    "network",
    "itsm_record",
    "generic",
)

SEVERITIES: Tuple[str, ...] = ("critical", "high", "medium", "low")
DEPLOY_POLICIES: Tuple[str, ...] = ("window", "immediate")

TOPIC_WINDOWS_PUBLISHED = "maintenance:windows_published"
TOPIC_OVERRIDE_STARTED = "maintenance:override_started"
TOPIC_OVERRIDE_ENDED = "maintenance:override_ended"
TOPIC_WINDOW_MISSING = "maintenance:window_missing"
TOPIC_OUTAGE_PERSISTED = "monitoring:outage_persisted"
TOPIC_OUTAGE_RECOVERED = "monitoring:outage_recovered"

#: Minutes an override keeps counting as active after ``override_ended``.
DEFAULT_LINGER_MINUTES = 15
#: Upper bound of an override whose end is unknown when it starts (a deploy job).
DEFAULT_MAX_OVERRIDE_MINUTES = 240
#: How far ahead ``next_window``/``next_start`` look by default.
DEFAULT_HORIZON = timedelta(days=30)

#: Cron evaluation gives up after this span without a match (e.g. ``0 0 30 2 *``).
_CRON_SEARCH_LIMIT = timedelta(days=366 * 5)

UTC = timezone.utc

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_LABELS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
#: cron weekday names (0 = Sunday, unlike the weekly format where 0 = Monday).
_CRON_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class MaintenanceError(ValueError):
    """Base class for every validation error raised by this module."""


class ScheduleError(MaintenanceError):
    """A window definition is invalid (unknown type, bad time, bad cron, bad tz)."""


class RuleError(MaintenanceError):
    """An incident rule is invalid."""


# --------------------------------------------------------------------------- #
# Kinds and small helpers
# --------------------------------------------------------------------------- #


def is_valid_kind(kind: object) -> bool:
    """True for a lower-case slug (``^[a-z][a-z0-9_]*$``, max 32 chars).

    Kinds are the Entity-Registry kinds and deliberately **open**: a consumer
    accepts any valid slug from an event instead of checking a fixed list.
    """
    return isinstance(kind, str) and bool(_KIND_RE.match(kind))


def utcnow() -> datetime:
    """Aware UTC now, whole seconds (the bus carries second precision)."""
    return datetime.now(UTC).replace(microsecond=0)


def _zone(tz: str) -> ZoneInfo:
    if not isinstance(tz, str) or not tz:
        raise ScheduleError(
            "tz must be a non-empty IANA zone name such as 'Europe/Berlin'"
        )
    try:
        return ZoneInfo(tz)
    except (
        ZoneInfoNotFoundError,
        ValueError,
    ) as exc:  # pragma: no cover - message only
        raise ScheduleError(f"unknown time zone {tz!r}") from exc


def parse_iso(value: Union[str, datetime], *, default_tz: str = "UTC") -> datetime:
    """Parse an ISO-8601 timestamp into an **aware UTC** datetime.

    ``Z`` is accepted; a naive value is interpreted in ``default_tz``.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ScheduleError(f"invalid ISO-8601 timestamp {value!r}") from exc
    else:
        raise ScheduleError(f"invalid ISO-8601 timestamp {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(default_tz))
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    """Format an aware datetime as UTC with second precision and a ``Z`` suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_utc(local_naive: datetime, zone: ZoneInfo) -> datetime:
    """Attach ``zone`` to a naive local time and convert to UTC.

    ``fold=0`` picks the first occurrence of an ambiguous (autumn) time; a
    non-existent (spring) time is normalised forward by ``zoneinfo``.
    """
    return local_naive.replace(tzinfo=zone, fold=0).astimezone(UTC)


def _parse_hhmm(value: object, field_name: str) -> time:
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        raise ScheduleError(f"{field_name} must be 'HH:MM' (24 h), got {value!r}")
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _parse_day(value: object) -> int:
    if isinstance(value, bool):
        raise ScheduleError(f"invalid weekday {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ScheduleError(f"weekday {value} out of range 0 (Monday) … 6 (Sunday)")
    if isinstance(value, str):
        key = value.strip().lower()[:3]
        if key in _DAY_NAMES:
            return _DAY_NAMES[key]
        if value.strip().isdigit():
            return _parse_day(int(value.strip()))
    raise ScheduleError(f"invalid weekday {value!r} (use mon…sun or 0…6, 0 = Monday)")


def _as_int(value: object, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        else:
            raise ScheduleError(f"{field_name} must be an integer, got {value!r}")
    assert isinstance(value, int)
    if value < minimum:
        raise ScheduleError(f"{field_name} must be >= {minimum}, got {value}")
    return value


def _as_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in (
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
    ):
        return value.strip().lower() in ("true", "yes", "on")
    raise RuleError(f"{field_name} must be a boolean, got {value!r}")


# --------------------------------------------------------------------------- #
# Schedules (the normalised window model)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WeeklySchedule:
    """Recurring window on given weekdays. ``end <= start`` means "over midnight"."""

    days: frozenset[int]  # 0 = Monday … 6 = Sunday (ISO order)
    start: time
    end: time
    tz: str


@dataclass(frozen=True)
class OnceSchedule:
    """A single window; ``start``/``end`` are aware UTC datetimes."""

    start: datetime
    end: datetime
    note: Optional[str] = None


@dataclass(frozen=True)
class CronSchedule:
    """A window that opens at every cron fire for ``duration_minutes``."""

    expr: str
    duration_minutes: int
    tz: str


Schedule = Union[WeeklySchedule, OnceSchedule, CronSchedule]


class Interval(NamedTuple):
    """A concrete window occurrence, aware UTC, ``start < end``."""

    start: datetime
    end: datetime


def _body(raw: Mapping[str, object]) -> Tuple[str, Mapping[str, object]]:
    """Return ``(type, body)`` for the input form ``{"weekly": {...}}`` or the
    normal form ``{"type": "weekly", ...}``."""
    if not isinstance(raw, Mapping):
        raise ScheduleError(f"window must be a mapping, got {type(raw).__name__}")
    if "type" in raw:
        kind = raw["type"]
        if kind not in ("weekly", "once", "cron"):
            raise ScheduleError(f"unknown window type {kind!r} (weekly | once | cron)")
        return str(kind), raw
    present = [k for k in ("weekly", "once", "cron") if k in raw]
    if len(present) != 1:
        raise ScheduleError(
            "window must have exactly one of the keys 'weekly', 'once', 'cron'"
            f" (got {sorted(str(k) for k in raw)})"
        )
    body = raw[present[0]]
    if not isinstance(body, Mapping):
        raise ScheduleError(
            f"'{present[0]}' must be a mapping, got {type(body).__name__}"
        )
    return present[0], body


def parse_window(raw: Mapping[str, object], *, default_tz: str = "UTC") -> Schedule:
    """Parse one window (input form or normal form) into a schedule.

    Raises :class:`ScheduleError` with a message that names the problem.
    Unknown keys are ignored (consumers tolerate newer emitters).
    """
    kind, body = _body(raw)
    tz = body.get("tz", default_tz)
    if kind == "weekly":
        days_raw = body.get("days")
        if not isinstance(days_raw, (list, tuple)) or not days_raw:
            raise ScheduleError("weekly.days must be a non-empty list (mon…sun or 0…6)")
        days = frozenset(_parse_day(d) for d in days_raw)
        start = _parse_hhmm(body.get("start"), "weekly.start")
        end = _parse_hhmm(body.get("end"), "weekly.end")
        if start == end:
            raise ScheduleError(
                "weekly.end must differ from weekly.start (use 00:00–23:59 for a full day)"
            )
        zone_name = str(tz)
        _zone(zone_name)
        return WeeklySchedule(days=days, start=start, end=end, tz=zone_name)
    if kind == "once":
        start_raw, end_raw = body.get("start"), body.get("end")
        if start_raw is None or end_raw is None:
            raise ScheduleError("once.start and once.end are required (ISO-8601)")
        start_dt = parse_iso(start_raw, default_tz=str(tz))  # type: ignore[arg-type]
        end_dt = parse_iso(end_raw, default_tz=str(tz))  # type: ignore[arg-type]
        if end_dt <= start_dt:
            raise ScheduleError("once.end must be after once.start")
        note = body.get("note")
        return OnceSchedule(
            start=start_dt, end=end_dt, note=str(note) if note is not None else None
        )
    # cron
    expr = body.get("expr")
    if not isinstance(expr, str) or not expr.strip():
        raise ScheduleError("cron.expr must be a five-field cron expression")
    CronExpr.parse(expr)  # validates
    duration = _as_int(body.get("duration_minutes"), "cron.duration_minutes", minimum=1)
    zone_name = str(tz)
    _zone(zone_name)
    return CronSchedule(
        expr=" ".join(expr.split()), duration_minutes=duration, tz=zone_name
    )


def parse_windows(
    raw: Optional[Sequence[Mapping[str, object]]], *, default_tz: str = "UTC"
) -> List[Schedule]:
    """Parse a list of windows; ``None``/empty → ``[]``. Errors name the index."""
    if raw is None:
        return []
    if isinstance(raw, Mapping) or not isinstance(raw, (list, tuple)):
        raise ScheduleError(f"windows must be a list, got {type(raw).__name__}")
    out: List[Schedule] = []
    for idx, item in enumerate(raw):
        try:
            out.append(parse_window(item, default_tz=default_tz))
        except ScheduleError as exc:
            raise ScheduleError(f"windows[{idx}]: {exc}") from exc
    return out


def validate_windows(raw: object, *, default_tz: str = "UTC") -> List[str]:
    """Return a list of error texts instead of raising (linters, UIs).

    Every window is checked, so one bad entry does not hide the others.
    """
    if raw is None:
        return []
    if isinstance(raw, Mapping) or not isinstance(raw, (list, tuple)):
        return [f"windows must be a list, got {type(raw).__name__}"]
    errors: List[str] = []
    for idx, item in enumerate(raw):
        try:
            parse_window(item, default_tz=default_tz)
        except ScheduleError as exc:
            errors.append(f"windows[{idx}]: {exc}")
    return errors


def to_dict(schedule: Schedule) -> dict:
    """Normal form of a schedule (what travels on the bus and sits in tables)."""
    if isinstance(schedule, WeeklySchedule):
        return {
            "type": "weekly",
            "days": [_DAY_LABELS[d] for d in sorted(schedule.days)],
            "start": schedule.start.strftime("%H:%M"),
            "end": schedule.end.strftime("%H:%M"),
            "tz": schedule.tz,
        }
    if isinstance(schedule, OnceSchedule):
        out: dict = {
            "type": "once",
            "start": iso(schedule.start),
            "end": iso(schedule.end),
        }
        if schedule.note is not None:
            out["note"] = schedule.note
        return out
    return {
        "type": "cron",
        "expr": schedule.expr,
        "duration_minutes": schedule.duration_minutes,
        "tz": schedule.tz,
    }


def to_dicts(schedules: Iterable[Schedule]) -> List[dict]:
    return [to_dict(s) for s in schedules]


# --------------------------------------------------------------------------- #
# Cron (five fields, Vixie semantics, no @-macros)
# --------------------------------------------------------------------------- #


def _parse_cron_field(
    text: str, lo: int, hi: int, names: Optional[Mapping[str, int]], label: str
) -> Tuple[frozenset[int], bool]:
    """Return ``(values, restricted)``; ``restricted`` is False for a bare ``*``."""

    def atom(token: str) -> int:
        t = token.strip().lower()
        if names and t in names:
            return names[t]
        if t.isdigit():
            return int(t)
        raise ScheduleError(f"cron {label}: invalid value {token!r}")

    values: set[int] = set()
    restricted = False
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"cron {label}: empty list element")
        step = 1
        rng = part
        if "/" in part:
            rng, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"cron {label}: invalid step {step_text!r}")
            step = int(step_text)
        if rng == "*":
            start, end = lo, hi
            # Vixie: only a bare "*" is unrestricted; "*/n" restricts the field.
            if "/" in part:
                restricted = True
        else:
            restricted = True
            if "-" in rng:
                a, b = rng.split("-", 1)
                start, end = atom(a), atom(b)
            else:
                start = atom(rng)
                end = hi if "/" in part else start
        if label == "weekday":
            start, end = (0 if start == 7 else start), (0 if end == 7 else end)
            if "-" in rng and end < start:  # e.g. fri-sun → 5,6,0
                values.update(range(start, 7))
                values.update(range(0, end + 1))
                continue
        if not (lo <= start <= hi and lo <= end <= hi):
            raise ScheduleError(
                f"cron {label}: value out of range {lo}…{hi} in {part!r}"
            )
        if end < start:
            raise ScheduleError(f"cron {label}: range end before start in {part!r}")
        values.update(range(start, end + 1, step))
    if label == "weekday":
        values = {0 if v == 7 else v for v in values}  # 7 is Sunday too
    if not values:  # pragma: no cover - defensive
        raise ScheduleError(f"cron {label}: no values")
    return frozenset(values), restricted


@dataclass(frozen=True)
class CronExpr:
    """A parsed five-field cron expression evaluated in **naive local time**."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday … 6 = Saturday
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, text: str) -> "CronExpr":
        fields = text.split()
        if len(fields) != 5:
            raise ScheduleError(
                f"cron expression needs 5 fields (minute hour day month weekday), got {len(fields)}"
            )
        if fields[0].startswith("@"):
            raise ScheduleError("cron @-macros are not supported")
        minutes, _ = _parse_cron_field(fields[0], 0, 59, None, "minute")
        hours, _ = _parse_cron_field(fields[1], 0, 23, None, "hour")
        days, dom_r = _parse_cron_field(fields[2], 1, 31, None, "day")
        months, _ = _parse_cron_field(fields[3], 1, 12, _MONTH_NAMES, "month")
        weekdays, dow_r = _parse_cron_field(fields[4], 0, 7, _CRON_DOW_NAMES, "weekday")
        return cls(
            text=" ".join(fields),
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            weekdays=weekdays,
            dom_restricted=dom_r,
            dow_restricted=dow_r,
        )

    def matches_day(self, d: date) -> bool:
        """Vixie rule: if day-of-month *and* weekday are restricted, either matches."""
        dom = d.day in self.days
        dow = ((d.weekday() + 1) % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom or dow
        if self.dom_restricted:
            return dom
        if self.dow_restricted:
            return dow
        return True

    def next_fire(self, after: datetime) -> Optional[datetime]:
        """First fire strictly after ``after`` (naive local), or None within the search limit."""
        t = after.replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        limit = t + _CRON_SEARCH_LIMIT
        while t <= limit:
            if t.month not in self.months:
                year, month = (
                    (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
                )
                t = datetime(year, month, 1)
                continue
            if not self.matches_day(t.date()):
                t = datetime.combine(t.date() + timedelta(days=1), time.min)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t
        return None

    def last_fire(
        self, before: datetime, *, not_before: datetime
    ) -> Optional[datetime]:
        """Latest fire at or before ``before`` that is not earlier than ``not_before``."""
        t = before.replace(second=0, microsecond=0, tzinfo=None)
        floor = not_before.replace(second=0, microsecond=0, tzinfo=None)
        while t >= floor:
            if t.month not in self.months:
                t = datetime(t.year, t.month, 1) - timedelta(minutes=1)
                continue
            if not self.matches_day(t.date()):
                t = datetime.combine(t.date(), time.min) - timedelta(minutes=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) - timedelta(minutes=1)
                continue
            if t.minute not in self.minutes:
                t -= timedelta(minutes=1)
                continue
            return t
        return None


# --------------------------------------------------------------------------- #
# Interval enumeration
# --------------------------------------------------------------------------- #


def _iter_weekly(
    s: WeeklySchedule, after: datetime, until: datetime
) -> Iterator[Interval]:
    zone = _zone(s.tz)
    day = after.astimezone(zone).date() - timedelta(days=1)
    last_day = until.astimezone(zone).date() + timedelta(days=1)
    over_midnight = s.end <= s.start
    while day <= last_day:
        if day.weekday() in s.days:
            start_local = datetime.combine(day, s.start)
            end_local = datetime.combine(
                day + timedelta(days=1) if over_midnight else day, s.end
            )
            start_utc, end_utc = _to_utc(start_local, zone), _to_utc(end_local, zone)
            # A spring-forward day can collapse a short window to nothing; skip it.
            if end_utc > start_utc and end_utc > after and start_utc <= until:
                yield Interval(start_utc, end_utc)
        day += timedelta(days=1)


def _iter_once(s: OnceSchedule, after: datetime, until: datetime) -> Iterator[Interval]:
    if s.end > after and s.start <= until:
        yield Interval(s.start, s.end)


def _iter_cron(s: CronSchedule, after: datetime, until: datetime) -> Iterator[Interval]:
    zone = _zone(s.tz)
    expr = CronExpr.parse(s.expr)
    duration = timedelta(minutes=s.duration_minutes)
    local_after = after.astimezone(zone).replace(tzinfo=None)
    # A fire shortly before `after` may still be running.
    cursor = expr.last_fire(local_after, not_before=local_after - duration)
    if cursor is not None:
        start_utc = _to_utc(cursor, zone)
        if start_utc + duration > after and start_utc <= until:
            yield Interval(start_utc, start_utc + duration)
    else:
        cursor = local_after
    while True:
        nxt = expr.next_fire(cursor)
        if nxt is None:
            return
        start_utc = _to_utc(nxt, zone)
        if start_utc > until:
            return
        yield Interval(start_utc, start_utc + duration)
        cursor = nxt


def _iter_schedule(s: Schedule, after: datetime, until: datetime) -> Iterator[Interval]:
    if isinstance(s, WeeklySchedule):
        return _iter_weekly(s, after, until)
    if isinstance(s, OnceSchedule):
        return _iter_once(s, after, until)
    return _iter_cron(s, after, until)


def _ensure_aware(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is None:
        raise MaintenanceError(f"{name} must be an aware datetime")
    return dt.astimezone(UTC)


def _merged(
    schedules: Iterable[Schedule], after: datetime, until: datetime
) -> Iterator[Interval]:
    """All occurrences of all schedules with ``end > after`` and ``start <= until``,
    ascending by start (k-way merge)."""
    heap: List[Tuple[datetime, int, Interval, Iterator[Interval]]] = []
    for idx, s in enumerate(schedules):
        it = _iter_schedule(s, after, until)
        first = next(it, None)
        if first is not None:
            heap.append((first.start, idx, first, it))
    heapq.heapify(heap)
    while heap:
        _, idx, interval, it = heapq.heappop(heap)
        yield interval
        following = next(it, None)
        if following is not None:
            heapq.heappush(heap, (following.start, idx, following, it))


def _coalesce(intervals: Iterable[Interval]) -> Iterator[Interval]:
    """Merge overlapping or adjacent intervals (input ascending by start)."""
    current: Optional[Interval] = None
    for iv in intervals:
        if current is None:
            current = iv
        elif iv.start <= current.end:
            if iv.end > current.end:
                current = Interval(current.start, iv.end)
        else:
            yield current
            current = iv
    if current is not None:
        yield current


def intervals_between(
    schedules: Iterable[Schedule], start: datetime, end: datetime, *, limit: int = 500
) -> List[Interval]:
    """Coalesced window occurrences overlapping ``[start, end)``, ascending.

    Stops after ``limit`` raw occurrences (a ``*/5 * * * *`` schedule over a
    month would otherwise be large); the result then covers only the beginning.
    """
    start, end = _ensure_aware(start, "start"), _ensure_aware(end, "end")
    raw: List[Interval] = []
    for iv in _merged(schedules, start, end):
        if iv.start >= end:
            break
        raw.append(iv)
        if len(raw) >= limit:
            break
    return list(_coalesce(raw))


def next_window(
    schedules: Iterable[Schedule],
    after: datetime,
    *,
    horizon: timedelta = DEFAULT_HORIZON,
) -> Optional[Interval]:
    """Earliest (coalesced) window with ``end > after`` starting within ``horizon``.

    If ``after`` lies inside a window, that running window is returned
    (``start <= after``), extended by adjacent/overlapping occurrences.
    """
    after = _ensure_aware(after, "after")
    until = after + horizon
    current: Optional[Interval] = None
    for iv in _merged(schedules, after, until):
        if current is None:
            current = iv
        elif iv.start <= current.end:
            if iv.end > current.end:
                current = Interval(current.start, iv.end)
        else:
            break
    return current


def current_interval(
    schedules: Iterable[Schedule], now: datetime
) -> Optional[Interval]:
    """The window containing ``now`` (``start <= now < end``), coalesced, or None."""
    window = next_window(schedules, now)
    if window is not None and window.start <= _ensure_aware(now, "now"):
        return window
    return None


def active_until(schedules: Iterable[Schedule], now: datetime) -> Optional[datetime]:
    """End of the running window, or None when no window is active."""
    window = current_interval(schedules, now)
    return window.end if window else None


def next_start(
    schedules: Iterable[Schedule],
    after: datetime,
    *,
    horizon: timedelta = DEFAULT_HORIZON,
) -> Optional[datetime]:
    """Start of the next window that is **not yet running** at ``after``."""
    after = _ensure_aware(after, "after")
    schedules = list(schedules)
    running = current_interval(schedules, after)
    base = running.end if running else after
    window = next_window(schedules, base, horizon=horizon)
    if window is None or window.start <= after:
        return None
    return window.start


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #


@dataclass
class Override:
    """A temporary maintenance marker (a running deploy, a manual override).

    Active from ``started_at`` until ``until``; once ``ended_at`` is known the
    override lingers for ``linger_minutes`` after it (containers restart, probes
    settle) and then expires.
    """

    override_id: str
    ci_ids: Tuple[str, ...]
    until: datetime
    started_at: datetime
    ended_at: Optional[datetime] = None
    linger_minutes: int = DEFAULT_LINGER_MINUTES
    reason: str = ""
    source: str = ""


def override_until(o: Override) -> datetime:
    """Effective end: ``ended_at + linger`` once ended, otherwise ``until``."""
    if o.ended_at is not None:
        return o.ended_at + timedelta(minutes=max(0, o.linger_minutes))
    return o.until


def override_active(o: Override, now: datetime) -> bool:
    now = _ensure_aware(now, "now")
    return o.started_at <= now < override_until(o)


def override_from_payload(payload: Mapping[str, object]) -> Override:
    """Build an :class:`Override` from a ``maintenance:override_started`` payload."""
    override_id = payload.get("override_id")
    if not isinstance(override_id, str) or not override_id:
        raise MaintenanceError("override_started: override_id is required")
    ci_ids_raw = payload.get("ci_ids") or ()
    if not isinstance(ci_ids_raw, (list, tuple)):
        raise MaintenanceError("override_started: ci_ids must be a list")
    until_raw = payload.get("until")
    if until_raw is None:
        raise MaintenanceError("override_started: until is required")
    started_raw = payload.get("started_at")
    started = parse_iso(started_raw) if started_raw is not None else utcnow()  # type: ignore[arg-type]
    linger_raw = payload.get("linger_minutes", DEFAULT_LINGER_MINUTES)
    linger = (
        _as_int(linger_raw, "linger_minutes", minimum=0)
        if linger_raw is not None
        else DEFAULT_LINGER_MINUTES
    )
    return Override(
        override_id=override_id,
        ci_ids=tuple(str(c) for c in ci_ids_raw),
        until=parse_iso(until_raw),  # type: ignore[arg-type]
        started_at=started,
        ended_at=None,
        linger_minutes=linger,
        reason=str(payload.get("reason") or ""),
        source=str(payload.get("source") or ""),
    )


def apply_override_ended(o: Override, payload: Mapping[str, object]) -> Override:
    """Return a copy of ``o`` with ``ended_at``/``linger_minutes`` from an
    ``override_ended`` payload (``ended_at`` missing → now)."""
    ended_raw = payload.get("ended_at")
    ended = parse_iso(ended_raw) if ended_raw is not None else utcnow()  # type: ignore[arg-type]
    linger_raw = payload.get("linger_minutes")
    linger = (
        _as_int(linger_raw, "linger_minutes", minimum=0)
        if linger_raw is not None
        else o.linger_minutes
    )
    return replace(o, ended_at=ended, linger_minutes=linger)


# --------------------------------------------------------------------------- #
# Effective state
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MaintenanceState:
    """What a consumer shows/acts on for one CI at one instant."""

    active: bool
    until: Optional[datetime]
    source: Optional[Literal["window", "override"]]
    next_start: Optional[datetime]
    note: Optional[str] = None


def effective_state(
    schedules: Iterable[Schedule],
    overrides: Iterable[Override],
    now: datetime,
    *,
    horizon: timedelta = DEFAULT_HORIZON,
) -> MaintenanceState:
    """Combine windows and overrides. An active override wins (``source="override"``);
    ``until`` is the later of the two ends."""
    now = _ensure_aware(now, "now")
    schedules = list(schedules)
    window = current_interval(schedules, now)
    active_overrides = [o for o in overrides if override_active(o, now)]
    upcoming = next_start(schedules, now, horizon=horizon)
    if active_overrides:
        strongest = max(active_overrides, key=override_until)
        until = override_until(strongest)
        if window is not None and window.end > until:
            until = window.end
        return MaintenanceState(
            True, until, "override", upcoming, strongest.reason or None
        )
    if window is not None:
        note = next(
            (
                s.note
                for s in schedules
                if isinstance(s, OnceSchedule) and s.start <= now < s.end and s.note
            ),
            None,
        )
        return MaintenanceState(True, window.end, "window", upcoming, note)
    return MaintenanceState(False, None, None, upcoming, None)


# --------------------------------------------------------------------------- #
# Incident rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IncidentRule:
    """Open an incident when a CI is down for ``offline_minutes`` outside maintenance."""

    enabled: bool
    offline_minutes: int
    severity: str
    auto_resolve: bool


def parse_incident_rule(
    raw: Optional[Mapping[str, object]], *, fallback: Optional[IncidentRule] = None
) -> Optional[IncidentRule]:
    """Parse an ``incidents`` block; missing keys come from ``fallback``.

    ``offline_minutes: 0`` disables the rule. A block that only says
    ``enabled: false`` is valid without ``offline_minutes``.
    """
    if raw is None:
        return fallback
    if not isinstance(raw, Mapping):
        raise RuleError(f"incidents must be a mapping, got {type(raw).__name__}")
    enabled_default = fallback.enabled if fallback else True
    enabled = _as_bool(raw.get("enabled", enabled_default), "incidents.enabled")
    offline_raw = raw.get(
        "offline_minutes", fallback.offline_minutes if fallback else None
    )
    if offline_raw is None:
        if enabled:
            raise RuleError(
                "incidents.offline_minutes is required when the rule is enabled"
            )
        offline = 0
    else:
        try:
            offline = _as_int(offline_raw, "incidents.offline_minutes", minimum=0)
        except ScheduleError as exc:
            raise RuleError(str(exc)) from exc
    severity = raw.get("severity", fallback.severity if fallback else "high")
    if severity not in SEVERITIES:
        raise RuleError(
            f"incidents.severity must be one of {', '.join(SEVERITIES)}, got {severity!r}"
        )
    auto_default = fallback.auto_resolve if fallback else True
    auto_resolve = _as_bool(
        raw.get("auto_resolve", auto_default), "incidents.auto_resolve"
    )
    if offline == 0:
        enabled = False
    return IncidentRule(
        enabled=enabled,
        offline_minutes=offline,
        severity=str(severity),
        auto_resolve=auto_resolve,
    )


def resolve_incident_rule(
    own: Optional[Mapping[str, object]],
    kind: str,
    *defaults_by_kind: Optional[Mapping[str, Mapping[str, object]]],
) -> Optional[IncidentRule]:
    """Cascade: own block (partial, filled from the default) → first
    ``incident_defaults`` mapping that defines ``kind`` → None.

    ``defaults_by_kind`` are passed most specific first (stage, then site).
    """
    fallback: Optional[IncidentRule] = None
    for defaults in defaults_by_kind:
        if defaults and isinstance(defaults, Mapping) and kind in defaults:
            fallback = parse_incident_rule(defaults[kind])
            break
    return parse_incident_rule(own, fallback=fallback)


def rule_to_dict(rule: IncidentRule) -> dict:
    return {
        "enabled": rule.enabled,
        "offline_minutes": rule.offline_minutes,
        "severity": rule.severity,
        "auto_resolve": rule.auto_resolve,
    }


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #


def make_outage_id(ci_id: str, since: datetime) -> str:
    """Deterministic id of one outage: ``"<ci_id>:<since ISO UTC>"`` (ITSM ``external_id``)."""
    return f"{ci_id}:{iso(since)}"


def make_window_missing_id(ci_id: str, job_ref: Union[str, int]) -> str:
    """Deterministic id of one "no window in horizon" event (ITSM ``external_id``)."""
    return f"window_missing:{ci_id}:{job_ref}"


# --------------------------------------------------------------------------- #
# Payload shapes (documentation + type checking; no runtime validation here)
# --------------------------------------------------------------------------- #


class IncidentRuleDict(TypedDict):
    enabled: bool
    offline_minutes: int
    severity: str
    auto_resolve: bool


class WindowsPublishedItem(TypedDict):
    ci_id: str
    kind: str
    name: str
    windows: List[dict]  # normal form, see to_dict()
    inherited_from: Optional[str]  # None | "host" | "stage_default" | "site_default"
    host_ci_id: NotRequired[Optional[str]]
    deploy_policy: NotRequired[str]  # "window" | "immediate"
    incidents: NotRequired[Optional[IncidentRuleDict]]


class WindowsPublished(TypedDict):
    """``maintenance:windows_published`` — the emitter's complete set; ``replaces``
    tells consumers to drop every earlier item of this ``source``."""

    source: str
    published_at: str
    replaces: bool
    items: List[WindowsPublishedItem]


class OverrideStarted(TypedDict):
    """``maintenance:override_started``."""

    override_id: str
    ci_ids: List[str]
    until: str
    reason: str
    source: str
    started_at: str
    linger_minutes: int


class OverrideEnded(TypedDict):
    """``maintenance:override_ended``."""

    override_id: str
    source: str
    ended_at: str
    linger_minutes: int


class WindowMissing(TypedDict):
    """``maintenance:window_missing`` — a ``deploy_policy: window`` deploy ran at
    once because no window lies within the horizon; ITSM opens a task."""

    event_id: str
    ci_id: str
    kind: str
    name: str
    site: str
    stage: str
    horizon_days: int
    job_id: Union[str, int]
    source: str
    detected_at: str
    reason: str
    host_ci_id: NotRequired[Optional[str]]


class OutagePersisted(TypedDict):
    """``monitoring:outage_persisted`` — once per outage, after ``offline_minutes``
    outside maintenance."""

    outage_id: str
    ci_id: str
    kind: str
    name: str
    monitor_id: Union[str, int]
    since: str
    persisted_at: str
    duration_s: int
    rule: IncidentRuleDict
    host_ci_id: NotRequired[Optional[str]]
    error: NotRequired[Optional[str]]


class OutageRecovered(TypedDict):
    """``monitoring:outage_recovered``."""

    outage_id: str
    ci_id: str
    since: str
    recovered_at: str
    duration_s: int


__all__ = [
    # constants
    "KNOWN_KINDS",
    "SEVERITIES",
    "DEPLOY_POLICIES",
    "TOPIC_WINDOWS_PUBLISHED",
    "TOPIC_OVERRIDE_STARTED",
    "TOPIC_OVERRIDE_ENDED",
    "TOPIC_WINDOW_MISSING",
    "TOPIC_OUTAGE_PERSISTED",
    "TOPIC_OUTAGE_RECOVERED",
    "DEFAULT_LINGER_MINUTES",
    "DEFAULT_MAX_OVERRIDE_MINUTES",
    "DEFAULT_HORIZON",
    "UTC",
    # errors
    "MaintenanceError",
    "ScheduleError",
    "RuleError",
    # kinds / time
    "is_valid_kind",
    "utcnow",
    "parse_iso",
    "iso",
    # schedules
    "WeeklySchedule",
    "OnceSchedule",
    "CronSchedule",
    "Schedule",
    "Interval",
    "CronExpr",
    "parse_window",
    "parse_windows",
    "validate_windows",
    "to_dict",
    "to_dicts",
    "intervals_between",
    "next_window",
    "current_interval",
    "active_until",
    "next_start",
    # overrides
    "Override",
    "override_until",
    "override_active",
    "override_from_payload",
    "apply_override_ended",
    # state
    "MaintenanceState",
    "effective_state",
    # incident rules
    "IncidentRule",
    "parse_incident_rule",
    "resolve_incident_rule",
    "rule_to_dict",
    # ids
    "make_outage_id",
    "make_window_missing_id",
    # payload shapes
    "IncidentRuleDict",
    "WindowsPublishedItem",
    "WindowsPublished",
    "OverrideStarted",
    "OverrideEnded",
    "WindowMissing",
    "OutagePersisted",
    "OutageRecovered",
]
