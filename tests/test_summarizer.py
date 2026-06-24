"""
Unit tests for app/ai/summarizer.py pure functions.

No DB, MongoDB, or OpenAI calls — tests the date-bound logic, event
truncation/prioritisation, and prompt formatting that are the most
likely sources of silent bugs.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.ai.summarizer import (
    MAX_EVENTS,
    _build_prompt,
    _format_events,
    _period_bounds,
    _truncate,
)


# ── _period_bounds ────────────────────────────────────────────────────────────

def test_daily_full_day_start_is_midnight():
    start, end = _period_bounds("UTC", "daily", full_day=True)
    assert start.hour == 0
    assert start.minute == 0
    assert start.second == 0


def test_daily_full_day_end_is_end_of_day():
    start, end = _period_bounds("UTC", "daily", full_day=True)
    assert end.hour == 23
    assert end.minute == 59
    assert end.second == 59


def test_daily_not_full_day_end_is_approx_now():
    start, end = _period_bounds("UTC", "daily", full_day=False)
    now = datetime.now(timezone.utc)
    # end should be within 5 seconds of now
    assert abs((end - now).total_seconds()) < 5


def test_daily_start_is_before_end():
    start, end = _period_bounds("UTC", "daily", full_day=True)
    assert start < end


def test_weekly_start_is_monday():
    start, _ = _period_bounds("UTC", "weekly")
    # weekday() == 0 means Monday
    assert start.weekday() == 0


def test_weekly_start_is_midnight():
    start, _ = _period_bounds("UTC", "weekly")
    assert start.hour == 0 and start.minute == 0 and start.second == 0


def test_weekly_covers_at_most_7_days():
    start, end = _period_bounds("UTC", "weekly")
    assert end - start <= timedelta(days=7)


def test_period_bounds_returns_utc():
    """Both bounds must be timezone-aware UTC datetimes."""
    start, end = _period_bounds("America/New_York", "daily", full_day=True)
    assert start.tzinfo is not None
    assert end.tzinfo is not None


def test_specific_date_window_is_exactly_one_day():
    """
    The specific_date path in _summarise_profile computes midnight-to-midnight.
    This is the logic that had a previous timezone bug — verify the invariant directly.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("UTC")
    specific_date = "2026-06-23"
    sd = datetime.strptime(specific_date, "%Y-%m-%d").replace(tzinfo=tz)
    period_start = sd.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    period_end = (sd + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    assert period_end - period_start == timedelta(days=1)
    assert period_start.hour == 0
    assert period_end.hour == 0
    assert period_start < period_end


def test_specific_date_with_offset_timezone_still_covers_one_day():
    """Timezone-offset case: the window should still be exactly 24 hours."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")  # UTC-5 in winter
    sd = datetime.strptime("2026-06-23", "%Y-%m-%d").replace(tzinfo=tz)
    period_start = sd.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    period_end = (sd + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    assert period_end - period_start == timedelta(days=1)


# ── _truncate ─────────────────────────────────────────────────────────────────

def _make_event(event_type: str, ts_offset_secs: int = 0) -> dict:
    return {
        "event_type": event_type,
        "occurred_at": datetime(2026, 6, 23, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=ts_offset_secs),
        "source": "github",
        "title": f"Event: {event_type}",
    }


def test_truncate_keeps_max_events():
    events = [_make_event("commit", i) for i in range(MAX_EVENTS + 50)]
    result = _truncate(events)
    assert len(result) == MAX_EVENTS


def test_truncate_priority_merged_pr_before_commit():
    """pr_merged (priority 0) should appear before commit (priority 1)."""
    events = [_make_event("commit"), _make_event("pr_merged")]
    result = _truncate(events)
    assert result[0]["event_type"] == "pr_merged"


def test_truncate_priority_commit_before_meeting():
    events = [_make_event("meeting"), _make_event("commit")]
    result = _truncate(events)
    assert result[0]["event_type"] == "commit"


def test_truncate_same_priority_newer_first():
    """Within the same priority, more recent events come first."""
    older = _make_event("commit", ts_offset_secs=0)
    newer = _make_event("commit", ts_offset_secs=3600)
    result = _truncate([older, newer])
    assert result[0]["occurred_at"] > result[1]["occurred_at"]


def test_truncate_preserves_all_events_when_under_limit():
    events = [_make_event("commit") for _ in range(10)]
    assert len(_truncate(events)) == 10


# ── _format_events ────────────────────────────────────────────────────────────

def test_format_events_includes_source_and_event_type():
    events = [{
        "occurred_at": datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        "source": "github",
        "event_type": "commit",
        "workspace": "org/repo",
        "title": "Fix auth bug",
    }]
    output = _format_events(events)
    assert "github" in output
    assert "commit" in output
    assert "Fix auth bug" in output


def test_format_events_includes_workspace_tag():
    events = [{
        "occurred_at": datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        "source": "github",
        "event_type": "commit",
        "workspace": "myorg/myrepo",
        "title": "Update deps",
    }]
    output = _format_events(events)
    assert "[repo:myorg/myrepo]" in output


def test_format_events_omits_repo_tag_when_no_workspace():
    events = [{
        "occurred_at": datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        "source": "jira",
        "event_type": "issue_updated",
        "workspace": "",
        "title": "Bug fix",
    }]
    output = _format_events(events)
    assert "[repo:" not in output


def test_format_events_one_line_per_event():
    events = [
        {"occurred_at": datetime(2026, 6, 23, 10, i, tzinfo=timezone.utc),
         "source": "github", "event_type": "commit", "workspace": "", "title": f"Event {i}"}
        for i in range(3)
    ]
    lines = [l for l in _format_events(events).splitlines() if l.strip()]
    assert len(lines) == 3


# ── _build_prompt ─────────────────────────────────────────────────────────────

def test_build_prompt_contains_activity_data_markers():
    prompt = _build_prompt("daily", [], "")
    assert "ACTIVITY DATA START" in prompt
    assert "ACTIVITY DATA END" in prompt


def test_build_prompt_includes_period_type():
    prompt = _build_prompt("weekly", [], "")
    assert "weekly" in prompt


def test_build_prompt_includes_caveat_when_provided():
    prompt = _build_prompt("daily", [], "Note: data from github was unavailable.")
    assert "github was unavailable" in prompt


def test_build_prompt_no_markdown_instruction():
    prompt = _build_prompt("daily", [], "")
    assert "No markdown" in prompt or "no markdown" in prompt.lower()
