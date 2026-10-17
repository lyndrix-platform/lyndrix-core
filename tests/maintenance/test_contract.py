"""Contract snapshot: constants, identifiers and payload shapes of core.api.maintenance.

A failing test here means an API change — bump ``__api_version__`` and the docs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.api import maintenance as m


def test_topics_and_constants_are_stable():
    assert m.TOPIC_WINDOWS_PUBLISHED == "maintenance:windows_published"
    assert m.TOPIC_OVERRIDE_STARTED == "maintenance:override_started"
    assert m.TOPIC_OVERRIDE_ENDED == "maintenance:override_ended"
    assert m.TOPIC_WINDOW_MISSING == "maintenance:window_missing"
    assert m.TOPIC_OUTAGE_PERSISTED == "monitoring:outage_persisted"
    assert m.TOPIC_OUTAGE_RECOVERED == "monitoring:outage_recovered"
    assert m.SEVERITIES == ("critical", "high", "medium", "low")
    assert m.DEPLOY_POLICIES == ("window", "immediate")
    assert m.DEFAULT_LINGER_MINUTES == 15
    assert m.DEFAULT_MAX_OVERRIDE_MINUTES == 240
    assert m.DEFAULT_HORIZON == timedelta(days=30)


def test_known_kinds_cover_the_registry_but_are_not_a_gate():
    for kind in (
        "hypervisor",
        "host",
        "service",
        "container",
        "network",
        "itsm_record",
        "generic",
    ):
        assert kind in m.KNOWN_KINDS
    assert "vm" not in m.KNOWN_KINDS
    # Decision A: kinds are open — a future kind passes without a core release.
    assert m.is_valid_kind("network")
    assert m.is_valid_kind("my_kind2")
    assert m.is_valid_kind("storage_pool")


@pytest.mark.parametrize(
    "bad", ["", "VM", "a-b", "1abc", "has space", None, 3, "x" * 33]
)
def test_invalid_kinds(bad):
    assert not m.is_valid_kind(bad)


def test_identifiers_are_deterministic():
    since = datetime(2026, 10, 18, 7, 5, 9, 123456, tzinfo=timezone.utc)
    ci = "3f0a9c2e-1111-4222-8333-444455556666"
    assert m.make_outage_id(ci, since) == f"{ci}:2026-10-18T07:05:09Z"
    assert m.make_outage_id(ci, since) == m.make_outage_id(ci, since)
    assert m.make_window_missing_id(ci, 42) == f"window_missing:{ci}:42"
    assert m.make_window_missing_id(ci, "42") == m.make_window_missing_id(ci, 42)


def test_iso_and_parse_iso_round_trip():
    dt = datetime(2026, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    text = m.iso(dt)
    assert text == "2026-03-01T10:00:00Z"
    assert m.parse_iso(text) == dt
    assert m.parse_iso("2026-03-01T10:00:00+00:00") == dt
    # naive → default tz
    assert m.parse_iso("2026-03-01T11:00", default_tz="Europe/Berlin") == dt
    assert m.parse_iso("2026-03-01T10:00") == dt  # naive defaults to UTC
    with pytest.raises(m.ScheduleError):
        m.parse_iso("not a date")
    with pytest.raises(m.ScheduleError):
        m.parse_iso("")


def test_payload_shapes_name_the_contract_fields():
    assert set(m.WindowsPublished.__annotations__) == {
        "source",
        "published_at",
        "replaces",
        "items",
    }
    assert set(m.WindowsPublishedItem.__annotations__) >= {
        "ci_id", "kind", "name", "windows", "inherited_from", "host_ci_id", "deploy_policy", "incidents"
    }  # fmt: skip
    assert set(m.OverrideStarted.__annotations__) == {
        "override_id", "ci_ids", "until", "reason", "source", "started_at", "linger_minutes"
    }  # fmt: skip
    assert set(m.OverrideEnded.__annotations__) == {
        "override_id",
        "source",
        "ended_at",
        "linger_minutes",
    }
    assert set(m.WindowMissing.__annotations__) >= {
        "event_id", "ci_id", "kind", "name", "site", "stage", "horizon_days", "job_id", "source", "detected_at", "reason"
    }  # fmt: skip
    assert set(m.OutagePersisted.__annotations__) >= {
        "outage_id", "ci_id", "kind", "name", "monitor_id", "since", "persisted_at", "duration_s", "rule"
    }  # fmt: skip
    assert set(m.OutageRecovered.__annotations__) == {
        "outage_id",
        "ci_id",
        "since",
        "recovered_at",
        "duration_s",
    }


def test_public_names_exist():
    for name in m.__all__:
        assert hasattr(m, name), name
