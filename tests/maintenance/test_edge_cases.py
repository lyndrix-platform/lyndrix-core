"""Edge cases of the schedule evaluation (audit finding BG-13).

* Windows whose occurrences never pause (overlapping/abutting cron fires, 24/7
  weekly windows) used to be walked across the whole 30-day horizon several
  times per call (~0.2–0.3 s) and reported ``until = now + 30 days`` plus a
  ``next_start`` inside the running window — both artefacts of the horizon.
* DST transitions in both directions for weekly and cron windows.
"""
import time as _time
from datetime import datetime, timedelta, timezone

import pytest

from core.api import maintenance as m

BERLIN = "Europe/Berlin"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _best_of(fn, runs: int = 5) -> float:
    best = float("inf")
    for _ in range(runs):
        t0 = _time.perf_counter()
        fn()
        best = min(best, _time.perf_counter() - t0)
    return best


# --- never-ending windows ---------------------------------------------------------

OVERLAPPING = [
    {"cron": {"expr": "*/5 * * * *", "duration_minutes": 10, "tz": BERLIN}},   # overlap
    {"cron": {"expr": "0 * * * *", "duration_minutes": 60, "tz": BERLIN}},     # abut exactly
]


@pytest.mark.parametrize("window", OVERLAPPING)
def test_never_ending_cron_is_open_ended_not_horizon_shaped(window):
    schedules = m.parse_windows([window])
    now = utc(2026, 11, 14, 12, 0, 30)

    st = m.effective_state(schedules, [], now)

    assert st.active and st.source == "window"
    assert st.open_ended is True
    # A lower bound backed by real occurrences — not "now + 30 days".
    assert now < st.until < now + m.DEFAULT_HORIZON - timedelta(days=1)
    assert st.next_start is None                       # nothing "starts next" inside a running window
    assert m.next_start(schedules, now) is None
    assert m.active_until(schedules, now) == st.until
    # It really is active right up to the reported bound, and beyond.
    assert m.effective_state(schedules, [], st.until - timedelta(seconds=1)).active
    assert m.effective_state(schedules, [], st.until).active


@pytest.mark.parametrize("window", OVERLAPPING)
def test_never_ending_cron_is_cheap(window):
    schedules = m.parse_windows([window])
    now = utc(2026, 11, 14, 12, 0, 30)
    assert _best_of(lambda: m.effective_state(schedules, [], now)) < 0.05      # was ~0.2–0.3 s
    assert _best_of(lambda: m.next_window(schedules, now)) < 0.05
    assert _best_of(lambda: m.next_start(schedules, now)) < 0.05


def test_always_on_weekly_windows_are_open_ended():
    every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    schedules = m.parse_windows(
        [{"weekly": {"days": every_day, "start": "00:00", "end": "12:00", "tz": BERLIN}},
         {"weekly": {"days": every_day, "start": "12:00", "end": "00:00", "tz": BERLIN}}]   # abut around the clock
    )
    now = utc(2026, 6, 10, 9)
    st = m.effective_state(schedules, [], now)
    assert st.active and st.open_ended and st.next_start is None
    assert st.until > now + timedelta(days=7)          # real coverage, flagged as a lower bound
    assert _best_of(lambda: m.effective_state(schedules, [], now)) < 0.05


def test_ordinary_windows_are_not_open_ended():
    schedules = m.parse_windows(
        [{"weekly": {"days": ["wed"], "start": "08:00", "end": "12:00", "tz": "UTC"}},
         {"cron": {"expr": "0 11 * * 3", "duration_minutes": 120, "tz": "UTC"}}]     # overlaps → one window
    )
    st = m.effective_state(schedules, [], utc(2026, 6, 10, 9))                         # a Wednesday
    assert st.active and st.open_ended is False
    assert st.until == utc(2026, 6, 10, 13)
    assert st.next_start == utc(2026, 6, 17, 8)
    idle = m.effective_state(schedules, [], utc(2026, 6, 11, 9))
    assert not idle.active and idle.open_ended is False and idle.next_start == utc(2026, 6, 17, 8)


def test_long_single_window_keeps_its_real_end_beyond_the_horizon():
    """A 60-day one-off is not "open-ended" just because it outlasts the 30-day horizon."""
    schedules = m.parse_windows(
        [{"once": {"start": "2026-06-01T00:00:00Z", "end": "2026-07-31T00:00:00Z", "note": "DC move"}}]
    )
    st = m.effective_state(schedules, [], utc(2026, 6, 2))
    assert st.active and st.open_ended is False
    assert st.until == utc(2026, 7, 31) and st.note == "DC move"
    assert st.next_start is None


def test_window_continued_past_the_horizon_reports_the_real_end():
    """Occurrences that start after the horizon still extend a RUNNING window."""
    schedules = m.parse_windows(
        [{"once": {"start": "2026-06-01T00:00:00Z", "end": "2026-07-05T00:00:00Z"}},
         {"once": {"start": "2026-07-04T00:00:00Z", "end": "2026-07-10T00:00:00Z"}}]   # starts 33 days in
    )
    st = m.effective_state(schedules, [], utc(2026, 6, 1, 1))
    assert st.until == utc(2026, 7, 10) and st.open_ended is False


def test_override_outlasting_a_window_is_a_real_end():
    schedules = m.parse_windows([OVERLAPPING[0]])
    now = utc(2026, 11, 14, 12)
    far = m.Override(override_id="o1", ci_ids=("ci",), until=now + timedelta(days=90), started_at=now - timedelta(hours=1))
    st = m.effective_state(schedules, [far], now)
    assert st.source == "override" and st.until == far.until and st.open_ended is False

    short = m.Override(override_id="o2", ci_ids=("ci",), until=now + timedelta(minutes=5), started_at=now - timedelta(hours=1))
    st = m.effective_state(schedules, [short], now)
    assert st.source == "override" and st.open_ended is True and st.until > short.until


def test_state_stays_positionally_constructible():
    """``open_ended`` is additive: existing 4/5-argument constructions keep working."""
    st = m.MaintenanceState(True, utc(2026, 1, 1), "window", None)
    assert st.open_ended is False and st.note is None


def test_cron_that_never_fires_answers_quickly():
    never = m.parse_windows([{"cron": {"expr": "0 0 30 2 *", "duration_minutes": 60, "tz": "UTC"}}])
    now = utc(2026, 1, 1)
    assert m.next_window(never, now) is None
    assert m.effective_state(never, [], now) == m.MaintenanceState(False, None, None, None, None)
    assert _best_of(lambda: m.effective_state(never, [], now), runs=3) < 0.25


# --- weekday notations ----------------------------------------------------------------

def test_weekday_range_and_sunday_as_seven_in_cron():
    fri_sun = m.parse_windows([{"cron": {"expr": "0 3 * * fri-sun", "duration_minutes": 30, "tz": "UTC"}}])
    starts = [iv.start for iv in m.intervals_between(fri_sun, utc(2026, 6, 8), utc(2026, 6, 15))]
    assert starts == [utc(2026, 6, 12, 3), utc(2026, 6, 13, 3), utc(2026, 6, 14, 3)]   # Fri, Sat, Sun

    seven = m.parse_windows([{"cron": {"expr": "0 3 * * 7", "duration_minutes": 30, "tz": "UTC"}}])
    zero = m.parse_windows([{"cron": {"expr": "0 3 * * 0", "duration_minutes": 30, "tz": "UTC"}}])
    span = (utc(2026, 6, 8), utc(2026, 6, 22))
    assert m.intervals_between(seven, *span) == m.intervals_between(zero, *span)
    assert [iv.start for iv in m.intervals_between(seven, *span)] == [utc(2026, 6, 14, 3), utc(2026, 6, 21, 3)]


# --- DST ----------------------------------------------------------------------------------
# Europe/Berlin 2026: spring forward Sun 29 Mar 02:00 → 03:00 (CET → CEST),
#                     fall back      Sun 25 Oct 03:00 → 02:00 (CEST → CET).

def test_weekly_window_on_the_fall_back_day_lasts_two_hours():
    s = m.parse_windows([{"weekly": {"days": ["sun"], "start": "02:00", "end": "03:00", "tz": BERLIN}}])
    (iv,) = m.intervals_between(s, utc(2026, 10, 24, 12), utc(2026, 10, 25, 12))
    # 02:00 CEST (first occurrence of the ambiguous hour) … 03:00 CET.
    assert iv == m.Interval(utc(2026, 10, 25, 0), utc(2026, 10, 25, 2))
    assert m.effective_state(s, [], utc(2026, 10, 25, 1, 30)).active     # the repeated hour is covered
    assert not m.effective_state(s, [], utc(2026, 10, 25, 2, 0)).active


def test_weekly_window_inside_the_spring_forward_gap_is_skipped():
    s = m.parse_windows([{"weekly": {"days": ["sun"], "start": "02:00", "end": "03:00", "tz": BERLIN}}])
    # 02:00–03:00 local does not exist on 29 Mar: no occurrence that day …
    assert m.intervals_between(s, utc(2026, 3, 28, 12), utc(2026, 3, 29, 12)) == []
    assert not m.effective_state(s, [], utc(2026, 3, 29, 1, 30)).active
    # … and the following Sunday is a normal one-hour window again (CEST = UTC+2).
    assert m.next_start(s, utc(2026, 3, 29, 12)) == utc(2026, 4, 5, 0)
    (iv,) = m.intervals_between(s, utc(2026, 4, 4, 12), utc(2026, 4, 5, 12))
    assert iv.end - iv.start == timedelta(hours=1)


def test_weekly_window_spanning_the_spring_gap_is_shortened_not_lost():
    s = m.parse_windows([{"weekly": {"days": ["sun"], "start": "01:00", "end": "04:00", "tz": BERLIN}}])
    (iv,) = m.intervals_between(s, utc(2026, 3, 28, 12), utc(2026, 3, 29, 12))
    assert iv == m.Interval(utc(2026, 3, 29, 0), utc(2026, 3, 29, 2))    # 3 wall-clock hours = 2 real ones


def test_cron_in_the_ambiguous_hour_fires_once_on_the_fall_back_day():
    s = m.parse_windows([{"cron": {"expr": "30 2 * * *", "duration_minutes": 30, "tz": BERLIN}}])
    ivs = m.intervals_between(s, utc(2026, 10, 24, 12), utc(2026, 10, 26, 12))
    assert [iv.start for iv in ivs] == [utc(2026, 10, 25, 0, 30), utc(2026, 10, 26, 1, 30)]   # CEST, then CET
    assert all(iv.end - iv.start == timedelta(minutes=30) for iv in ivs)


def test_cron_in_the_missing_hour_still_fires_once_on_the_spring_forward_day():
    s = m.parse_windows([{"cron": {"expr": "30 2 * * *", "duration_minutes": 30, "tz": BERLIN}}])
    ivs = m.intervals_between(s, utc(2026, 3, 28, 12), utc(2026, 3, 30, 12))
    # 02:30 does not exist on 29 Mar; zoneinfo normalises it forward (03:30 CEST = 01:30 UTC).
    assert [iv.start for iv in ivs] == [utc(2026, 3, 29, 1, 30), utc(2026, 3, 30, 0, 30)]
    assert all(iv.end - iv.start == timedelta(minutes=30) for iv in ivs)
