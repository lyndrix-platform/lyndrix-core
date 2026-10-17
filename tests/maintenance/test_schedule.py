"""Schedule parsing and time evaluation of core.api.maintenance (DST-safe, cron, overrides)."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from core.api import maintenance as m

UTC = timezone.utc
BERLIN = "Europe/Berlin"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parse_weekly_input_and_normal_form_are_equivalent():
    a = m.parse_window(
        {"weekly": {"days": ["sun"], "start": "02:00", "end": "04:00", "tz": BERLIN}}
    )
    b = m.parse_window(
        {
            "type": "weekly",
            "days": ["sun"],
            "start": "02:00",
            "end": "04:00",
            "tz": BERLIN,
        }
    )
    assert a == b == m.WeeklySchedule(frozenset({6}), time(2, 0), time(4, 0), BERLIN)
    assert m.to_dict(a) == {
        "type": "weekly",
        "days": ["sun"],
        "start": "02:00",
        "end": "04:00",
        "tz": BERLIN,
    }
    assert m.to_dict(m.parse_window(m.to_dict(a))) == m.to_dict(a)  # idempotent


def test_parse_weekly_days_as_numbers_names_and_full_names():
    s = m.parse_window(
        {
            "weekly": {
                "days": [0, "Tue", "wednesday", 6],
                "start": "22:00",
                "end": "02:00",
            }
        }
    )
    assert s.days == frozenset({0, 1, 2, 6})
    assert s.tz == "UTC"
    assert m.to_dict(s)["days"] == ["mon", "tue", "wed", "sun"]


def test_parse_once_and_cron():
    once = m.parse_window(
        {
            "once": {
                "start": "2026-11-02T22:00+01:00",
                "end": "2026-11-03T02:00+01:00",
                "note": "Storage-Umzug",
            }
        }
    )
    assert once == m.OnceSchedule(
        utc(2026, 11, 2, 21, 0), utc(2026, 11, 3, 1, 0), "Storage-Umzug"
    )
    assert m.to_dict(once) == {
        "type": "once",
        "start": "2026-11-02T21:00:00Z",
        "end": "2026-11-03T01:00:00Z",
        "note": "Storage-Umzug",
    }
    # naive once → default tz
    once_local = m.parse_window(
        {"once": {"start": "2026-11-02T22:00", "end": "2026-11-03T02:00"}},
        default_tz=BERLIN,
    )
    assert once_local.start == utc(2026, 11, 2, 21, 0)

    cron = m.parse_window(
        {"cron": {"expr": "0  2 1 * *", "duration_minutes": 180, "tz": BERLIN}}
    )
    assert cron == m.CronSchedule("0 2 1 * *", 180, BERLIN)
    assert m.to_dict(cron) == {
        "type": "cron",
        "expr": "0 2 1 * *",
        "duration_minutes": 180,
        "tz": BERLIN,
    }


def test_default_tz_applies_when_missing():
    s = m.parse_window(
        {"weekly": {"days": ["sat"], "start": "01:00", "end": "03:00"}},
        default_tz=BERLIN,
    )
    assert s.tz == BERLIN


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"type": "monthly", "days": [1]}, "unknown window type"),
        ({"weekly": {"days": [], "start": "02:00", "end": "04:00"}}, "weekly.days"),
        (
            {"weekly": {"days": ["sun"], "start": "2:00", "end": "04:00"}},
            "weekly.start",
        ),
        ({"weekly": {"days": ["sun"], "start": "02:00", "end": "24:00"}}, "weekly.end"),
        (
            {"weekly": {"days": ["sun"], "start": "02:00", "end": "02:00"}},
            "must differ",
        ),
        (
            {"weekly": {"days": ["funday"], "start": "02:00", "end": "04:00"}},
            "invalid weekday",
        ),
        ({"weekly": {"days": [7], "start": "02:00", "end": "04:00"}}, "out of range"),
        (
            {
                "weekly": {
                    "days": ["sun"],
                    "start": "02:00",
                    "end": "04:00",
                    "tz": "Mars/Olympus",
                }
            },
            "unknown time zone",
        ),
        (
            {"once": {"start": "2026-11-03T02:00Z", "end": "2026-11-02T22:00Z"}},
            "once.end must be after",
        ),
        ({"once": {"start": "2026-11-03T02:00Z"}}, "once.start and once.end"),
        ({"cron": {"expr": "0 2 1 *", "duration_minutes": 60}}, "5 fields"),
        ({"cron": {"expr": "0 2 1 * *", "duration_minutes": 0}}, "duration_minutes"),
        ({"cron": {"expr": "60 2 1 * *", "duration_minutes": 10}}, "out of range"),
        ({"cron": {"expr": "@daily", "duration_minutes": 10}}, "5 fields"),
        ({"weekly": {}, "once": {}}, "exactly one"),
        ({}, "exactly one"),
        ("weekly", "must be a mapping"),
    ],
)
def test_parse_errors_are_specific(raw, fragment):
    with pytest.raises(m.ScheduleError) as exc:
        m.parse_window(raw)
    assert fragment in str(exc.value)


def test_parse_windows_and_validate_windows():
    good = [{"weekly": {"days": ["sun"], "start": "02:00", "end": "04:00"}}]
    bad = good + [{"cron": {"expr": "x", "duration_minutes": 5}}]
    assert len(m.parse_windows(good)) == 1
    assert m.parse_windows(None) == []
    with pytest.raises(m.ScheduleError) as exc:
        m.parse_windows(bad)
    assert "windows[1]" in str(exc.value)
    errors = m.validate_windows(bad)
    assert len(errors) == 1 and errors[0].startswith("windows[1]")
    assert m.validate_windows({"weekly": {}}) == ["windows must be a list, got dict"]
    assert m.validate_windows(None) == []


# --------------------------------------------------------------------------- #
# Weekly windows in a DST zone
# --------------------------------------------------------------------------- #

SUNDAY_NIGHT = [
    m.parse_window(
        {"weekly": {"days": ["sun"], "start": "02:00", "end": "04:00", "tz": BERLIN}}
    )
]


def test_weekly_winter_and_summer_offsets():
    # 2026-01-11 is a Sunday (CET, +1): 02:00–04:00 local = 01:00–03:00Z
    assert m.next_window(SUNDAY_NIGHT, utc(2026, 1, 10, 12, 0)) == m.Interval(
        utc(2026, 1, 11, 1, 0), utc(2026, 1, 11, 3, 0)
    )
    # 2026-07-05 is a Sunday (CEST, +2): 00:00–02:00Z
    assert m.next_window(SUNDAY_NIGHT, utc(2026, 7, 4, 12, 0)) == m.Interval(
        utc(2026, 7, 5, 0, 0), utc(2026, 7, 5, 2, 0)
    )


def test_weekly_over_midnight_belongs_to_the_start_day():
    fri_night = [
        m.parse_window(
            {
                "weekly": {
                    "days": ["fri"],
                    "start": "22:00",
                    "end": "02:00",
                    "tz": BERLIN,
                }
            }
        )
    ]
    # 2026-01-09 is a Friday: 22:00 CET = 21:00Z … Sat 02:00 CET = 01:00Z
    w = m.next_window(fri_night, utc(2026, 1, 9, 12, 0))
    assert w == m.Interval(utc(2026, 1, 9, 21, 0), utc(2026, 1, 10, 1, 0))
    # at 00:30Z Saturday the Friday window is still running
    assert m.current_interval(fri_night, utc(2026, 1, 10, 0, 30)) == w
    assert m.current_interval(fri_night, utc(2026, 1, 10, 1, 0)) is None


def test_weekly_spring_forward_day_normalises_forward():
    # 2026-03-29 (Sunday) 02:00 does not exist in Berlin; zoneinfo (fold=0) treats
    # it as CET → 01:00Z, which is 03:00 CEST. The 02:00–04:00 window is 1 h long.
    w = m.next_window(SUNDAY_NIGHT, utc(2026, 3, 28, 12, 0))
    assert w == m.Interval(utc(2026, 3, 29, 1, 0), utc(2026, 3, 29, 2, 0))
    # A window that falls entirely into the gap (02:00–03:00) collapses and is skipped.
    gap = [
        m.parse_window(
            {
                "weekly": {
                    "days": ["sun"],
                    "start": "02:00",
                    "end": "03:00",
                    "tz": BERLIN,
                }
            }
        )
    ]
    w2 = m.next_window(gap, utc(2026, 3, 28, 12, 0))
    assert w2 is not None and w2.start.date() == utc(2026, 4, 5).date()


def test_weekly_fall_back_day_is_an_hour_longer():
    # 2026-10-25 (Sunday): 02:00 occurs twice; fold=0 picks the first (CEST → 00:00Z),
    # the end 04:00 CET = 03:00Z → the window lasts 3 h instead of 2 h.
    w = m.next_window(SUNDAY_NIGHT, utc(2026, 10, 24, 12, 0))
    assert w == m.Interval(utc(2026, 10, 25, 0, 0), utc(2026, 10, 25, 3, 0))


def test_weekly_multiple_days_and_intervals_between():
    s = [
        m.parse_window(
            {"weekly": {"days": ["mon", "wed"], "start": "01:00", "end": "02:00"}}
        )
    ]
    ivs = m.intervals_between(
        s, utc(2026, 1, 5), utc(2026, 1, 12)
    )  # Mon 5th … Mon 12th (excl.)
    assert ivs == [
        m.Interval(utc(2026, 1, 5, 1), utc(2026, 1, 5, 2)),
        m.Interval(utc(2026, 1, 7, 1), utc(2026, 1, 7, 2)),
    ]


# --------------------------------------------------------------------------- #
# Cron
# --------------------------------------------------------------------------- #


def test_cron_monthly_over_year_boundary():
    s = [
        m.parse_window(
            {"cron": {"expr": "0 2 1 * *", "duration_minutes": 180, "tz": BERLIN}}
        )
    ]
    w = m.next_window(s, utc(2026, 12, 15, 0, 0))
    assert w == m.Interval(utc(2027, 1, 1, 1, 0), utc(2027, 1, 1, 4, 0))
    # Inside the window (Jan 1 02:30Z) it is the current one; the next start (Feb 1)
    # lies 31 days ahead, outside the default 30-day horizon, inside a wider one.
    assert m.current_interval(s, utc(2027, 1, 1, 2, 30)) == w
    assert m.next_start(s, utc(2027, 1, 1, 2, 30)) is None
    assert m.next_start(s, utc(2027, 1, 1, 2, 30), horizon=timedelta(days=45)) == utc(
        2027, 2, 1, 1, 0
    )


def test_cron_every_15_minutes_coalesces_when_overlapping():
    short = [m.parse_window({"cron": {"expr": "*/15 * * * *", "duration_minutes": 10}})]
    ivs = m.intervals_between(short, utc(2026, 5, 1, 10, 0), utc(2026, 5, 1, 11, 0))
    assert [iv.start.minute for iv in ivs] == [0, 15, 30, 45]
    assert all(iv.end - iv.start == timedelta(minutes=10) for iv in ivs)
    long = [m.parse_window({"cron": {"expr": "*/15 * * * *", "duration_minutes": 20}})]
    ivs = m.intervals_between(long, utc(2026, 5, 1, 10, 0), utc(2026, 5, 1, 11, 0))
    assert len(ivs) == 1 and ivs[0].start <= utc(2026, 5, 1, 10, 0)


def test_cron_weekday_names_and_sunday_aliases():
    for expr in ("0 3 * * sun", "0 3 * * 0", "0 3 * * 7"):
        s = [m.parse_window({"cron": {"expr": expr, "duration_minutes": 60}})]
        # 2026-05-06 is a Wednesday → next Sunday is 2026-05-10
        assert m.next_window(s, utc(2026, 5, 6)) == m.Interval(
            utc(2026, 5, 10, 3), utc(2026, 5, 10, 4)
        )
    s = [m.parse_window({"cron": {"expr": "0 3 * * fri-sun", "duration_minutes": 60}})]
    assert m.next_window(s, utc(2026, 5, 6)).start == utc(2026, 5, 8, 3)  # Friday


def test_cron_vixie_or_between_day_and_weekday():
    # Both restricted: the 13th OR a Friday. 2026-02-06 is a Friday, 2026-02-13 a Friday too.
    both = [m.parse_window({"cron": {"expr": "0 0 13 * fri", "duration_minutes": 30}})]
    ivs = m.intervals_between(both, utc(2026, 2, 1), utc(2026, 2, 15))
    assert [iv.start.day for iv in ivs] == [6, 13]
    # Only day restricted → only the 13th.
    dom = [m.parse_window({"cron": {"expr": "0 0 13 * *", "duration_minutes": 30}})]
    assert [
        iv.start.day
        for iv in m.intervals_between(dom, utc(2026, 2, 1), utc(2026, 2, 15))
    ] == [13]
    # Month names, ranges with steps.
    s = m.CronExpr.parse("30 1-5/2 * jan,jul mon-fri")
    assert (
        s.hours == frozenset({1, 3, 5})
        and s.months == frozenset({1, 7})
        and s.weekdays == frozenset({1, 2, 3, 4, 5})
    )


def test_cron_last_fire_keeps_a_running_window_visible():
    s = [m.parse_window({"cron": {"expr": "0 2 * * *", "duration_minutes": 180}})]
    assert m.current_interval(s, utc(2026, 6, 1, 3, 0)) == m.Interval(
        utc(2026, 6, 1, 2), utc(2026, 6, 1, 5)
    )
    assert m.active_until(s, utc(2026, 6, 1, 4, 59)) == utc(2026, 6, 1, 5)
    assert m.current_interval(s, utc(2026, 6, 1, 5, 0)) is None
    assert m.next_start(s, utc(2026, 6, 1, 5, 0)) == utc(2026, 6, 2, 2)


def test_cron_that_never_fires_yields_nothing():
    never = [m.parse_window({"cron": {"expr": "0 0 30 2 *", "duration_minutes": 60}})]
    assert m.next_window(never, utc(2026, 1, 1)) is None
    assert (
        m.next_window(never, utc(2026, 1, 1), horizon=timedelta(days=365 * 6)) is None
    )
    assert m.CronExpr.parse("0 0 30 2 *").next_fire(datetime(2026, 1, 1)) is None


# --------------------------------------------------------------------------- #
# next_window / next_start / horizon
# --------------------------------------------------------------------------- #


def test_next_window_and_next_start_around_an_interval():
    s = [
        m.parse_window(
            {"once": {"start": "2026-06-10T10:00Z", "end": "2026-06-10T12:00Z"}}
        )
    ]
    iv = m.Interval(utc(2026, 6, 10, 10), utc(2026, 6, 10, 12))
    before, inside, after = (
        utc(2026, 6, 10, 9),
        utc(2026, 6, 10, 11),
        utc(2026, 6, 10, 13),
    )
    assert m.next_window(s, before) == iv and m.next_start(s, before) == iv.start
    assert m.next_window(s, inside) == iv and m.current_interval(s, inside) == iv
    assert m.next_start(s, inside) is None  # nothing after the running one
    assert m.next_window(s, after) is None and m.current_interval(s, after) is None
    assert m.next_window([], before) is None and m.next_start([], before) is None


def test_horizon_limits_the_search():
    s = [
        m.parse_window(
            {"once": {"start": "2026-07-20T10:00Z", "end": "2026-07-20T12:00Z"}}
        )
    ]
    now = utc(2026, 6, 1)
    assert m.next_window(s, now) is None  # 49 days ahead, default horizon 30
    assert m.next_window(s, now, horizon=timedelta(days=60)).start == utc(
        2026, 7, 20, 10
    )
    assert m.next_start(s, now, horizon=timedelta(days=60)) == utc(2026, 7, 20, 10)


def test_adjacent_windows_of_different_schedules_coalesce():
    a = m.parse_window(
        {"once": {"start": "2026-06-10T10:00Z", "end": "2026-06-10T12:00Z"}}
    )
    b = m.parse_window(
        {"once": {"start": "2026-06-10T12:00Z", "end": "2026-06-10T13:00Z"}}
    )
    c = m.parse_window(
        {"once": {"start": "2026-06-10T15:00Z", "end": "2026-06-10T16:00Z"}}
    )
    assert m.current_interval([a, b, c], utc(2026, 6, 10, 11)) == m.Interval(
        utc(2026, 6, 10, 10), utc(2026, 6, 10, 13)
    )
    assert m.next_start([a, b, c], utc(2026, 6, 10, 11)) == utc(2026, 6, 10, 15)
    assert m.intervals_between([c, a, b], utc(2026, 6, 10), utc(2026, 6, 11)) == [
        m.Interval(utc(2026, 6, 10, 10), utc(2026, 6, 10, 13)),
        m.Interval(utc(2026, 6, 10, 15), utc(2026, 6, 10, 16)),
    ]


def test_naive_query_times_are_rejected():
    with pytest.raises(m.MaintenanceError):
        m.next_window(SUNDAY_NIGHT, datetime(2026, 1, 1))


# --------------------------------------------------------------------------- #
# Overrides and effective state
# --------------------------------------------------------------------------- #


def _override(**kw) -> m.Override:
    base = dict(
        override_id="iac-job-7",
        ci_ids=("a",),
        until=utc(2026, 6, 1, 14, 0),
        started_at=utc(2026, 6, 1, 10, 0),
        reason="deploy",
        source="iac-orchestrator",
    )
    base.update(kw)
    return m.Override(**base)


def test_override_lifecycle():
    o = _override()
    assert not m.override_active(o, utc(2026, 6, 1, 9, 59))
    assert m.override_active(o, utc(2026, 6, 1, 10, 0))
    assert m.override_active(o, utc(2026, 6, 1, 13, 59))
    assert not m.override_active(o, utc(2026, 6, 1, 14, 0))  # until reached
    ended = m.apply_override_ended(
        o, {"ended_at": "2026-06-01T10:40:00Z", "linger_minutes": 15}
    )
    assert m.override_until(ended) == utc(2026, 6, 1, 10, 55)
    assert m.override_active(ended, utc(2026, 6, 1, 10, 54))
    assert not m.override_active(ended, utc(2026, 6, 1, 10, 55))
    assert o.ended_at is None  # original untouched


def test_override_from_payload():
    o = m.override_from_payload(
        {
            "override_id": "iac-job-9",
            "ci_ids": ["a", "b"],
            "until": "2026-06-01T14:00:00Z",
            "reason": "run now",
            "source": "iac-orchestrator",
            "started_at": "2026-06-01T10:00:00Z",
            "linger_minutes": 20,
        }
    )
    assert (
        o.ci_ids == ("a", "b")
        and o.until == utc(2026, 6, 1, 14)
        and o.linger_minutes == 20
    )
    minimal = m.override_from_payload(
        {"override_id": "x", "until": "2026-06-01T14:00:00Z"}
    )
    assert minimal.linger_minutes == m.DEFAULT_LINGER_MINUTES and minimal.ci_ids == ()
    with pytest.raises(m.MaintenanceError):
        m.override_from_payload({"until": "2026-06-01T14:00:00Z"})
    with pytest.raises(m.MaintenanceError):
        m.override_from_payload({"override_id": "x"})


def test_effective_state_window_only():
    s = [
        m.parse_window(
            {
                "once": {
                    "start": "2026-06-01T10:00Z",
                    "end": "2026-06-01T12:00Z",
                    "note": "disk swap",
                }
            }
        )
    ]
    st = m.effective_state(s, [], utc(2026, 6, 1, 11))
    assert st == m.MaintenanceState(
        True, utc(2026, 6, 1, 12), "window", None, "disk swap"
    )
    st = m.effective_state(s, [], utc(2026, 6, 1, 9))
    assert st == m.MaintenanceState(False, None, None, utc(2026, 6, 1, 10), None)


def test_effective_state_override_wins_and_until_is_the_later_end():
    s = [
        m.parse_window(
            {"once": {"start": "2026-06-01T10:00Z", "end": "2026-06-01T12:00Z"}}
        )
    ]
    short = _override(until=utc(2026, 6, 1, 11, 30))
    st = m.effective_state(s, [short], utc(2026, 6, 1, 11))
    assert (
        st.active
        and st.source == "override"
        and st.until == utc(2026, 6, 1, 12)
        and st.note == "deploy"
    )
    long = _override(until=utc(2026, 6, 1, 14))
    st = m.effective_state(s, [short, long], utc(2026, 6, 1, 11))
    assert st.until == utc(2026, 6, 1, 14)
    # override alone, outside any window
    st = m.effective_state([], [long], utc(2026, 6, 1, 13))
    assert st == m.MaintenanceState(
        True, utc(2026, 6, 1, 14), "override", None, "deploy"
    )


def test_effective_state_ignores_expired_and_ended_overrides():
    ended = m.apply_override_ended(_override(), {"ended_at": "2026-06-01T10:40:00Z"})
    assert m.effective_state(
        [], [ended], utc(2026, 6, 1, 10, 50)
    ).active  # within linger
    assert not m.effective_state([], [ended], utc(2026, 6, 1, 10, 56)).active
    assert not m.effective_state([], [_override()], utc(2026, 6, 1, 15)).active


# --------------------------------------------------------------------------- #
# Incident rules
# --------------------------------------------------------------------------- #


def test_parse_incident_rule_defaults_and_zero_disables():
    r = m.parse_incident_rule({"offline_minutes": 10})
    assert r == m.IncidentRule(True, 10, "high", True)
    assert (
        m.parse_incident_rule({"offline_minutes": 0, "severity": "low"}).enabled
        is False
    )
    assert m.parse_incident_rule({"enabled": False}) == m.IncidentRule(
        False, 0, "high", True
    )
    assert m.parse_incident_rule(None) is None
    fb = m.IncidentRule(True, 5, "critical", True)
    assert m.parse_incident_rule(None, fallback=fb) is fb
    assert m.parse_incident_rule({"severity": "medium"}, fallback=fb) == m.IncidentRule(
        True, 5, "medium", True
    )
    assert m.rule_to_dict(fb) == {
        "enabled": True,
        "offline_minutes": 5,
        "severity": "critical",
        "auto_resolve": True,
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"offline_minutes": 10, "severity": "urgent"},
        {"offline_minutes": -1},
        {"offline_minutes": "ten"},
        {},  # enabled by default but no offline_minutes
        {"offline_minutes": 5, "enabled": "maybe"},
        "nope",
    ],
)
def test_parse_incident_rule_errors(raw):
    with pytest.raises(m.RuleError):
        m.parse_incident_rule(raw)


def test_resolve_incident_rule_cascade_with_open_kinds():
    stage = {
        "service": {"offline_minutes": 10, "severity": "high"},
        "network": {"enabled": False},
    }
    site = {
        "service": {"offline_minutes": 30, "severity": "low"},
        "hypervisor": {"offline_minutes": 5, "severity": "critical"},
    }
    # own block wins, partial own is filled from the most specific default
    assert m.resolve_incident_rule(
        {"severity": "critical"}, "service", stage, site
    ) == m.IncidentRule(True, 10, "critical", True)
    # stage before site
    assert m.resolve_incident_rule(None, "service", stage, site).offline_minutes == 10
    # falls through to site for a kind the stage does not define
    assert (
        m.resolve_incident_rule(None, "hypervisor", stage, site).severity == "critical"
    )
    # any registry kind works as a key (decision A)
    assert m.resolve_incident_rule(None, "network", stage, site) == m.IncidentRule(
        False, 0, "high", True
    )
    # nothing defined anywhere → None (no rule)
    assert m.resolve_incident_rule(None, "storage_pool", stage, site) is None
    assert m.resolve_incident_rule(None, "service", None, None) is None
