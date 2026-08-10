"""Pins serialize_event — the wire shape of one activity event.

This shape used to be written out three times inside app/routes/activity.py
(/api/events/recent, /api/day-data, /api/week-breakdown). The expectations below
are transcribed from those three copies BEFORE they were merged, key order
included, because the endpoints have no response-body tests of their own.
"""
from datetime import datetime, timezone

from app.services.activity_query import iso_utc, serialize_event

T_NAIVE = datetime(2026, 7, 2, 9, 30, 0)
T_AWARE = datetime(2026, 7, 2, 9, 30, 0, tzinfo=timezone.utc)


def test_naive_timestamp_is_read_as_utc_not_local():
    assert iso_utc(T_NAIVE) == "2026-07-02T09:30:00+00:00"
    assert iso_utc(T_AWARE) == "2026-07-02T09:30:00+00:00"
    assert iso_utc("not-a-datetime") == "not-a-datetime"


def test_github_push_shape_and_key_order():
    e = {
        "source": "github", "event_type": "commit", "title": "fix login",
        "workspace": "acme/web", "occurred_at": T_NAIVE,
        "source_event_id": "abcdef1234567890",
        "raw_payload": {"head_commit": {"modified": ["a.py", "b.py"], "added": ["c.py"],
                                        "removed": []}},
    }
    out = serialize_event(e)
    assert list(out) == ["event_type", "title", "workspace", "occurred_at", "sha", "files"]
    assert out["sha"] == "abcdef1"
    assert out["files"] == ["a.py", "b.py", "c.py"]
    assert out["occurred_at"] == "2026-07-02T09:30:00+00:00"
    assert list({"source": e["source"], **out}) == [
        "source", "event_type", "title", "workspace", "occurred_at", "sha", "files"]


def test_jira_extras_are_merged_in():
    e = {
        "source": "jira", "event_type": "issue_updated", "title": "PROJ-1 done",
        "workspace": "PROJ", "occurred_at": T_AWARE,
        "raw_payload": {"issue": {"key": "PROJ-1", "fields": {
            "status": {"name": "Done"}, "priority": {"name": "High"},
            "assignee": {"displayName": "Sam"}}}},
    }
    out = serialize_event(e)
    assert out["issue_key"] == "PROJ-1"
    assert out["status"] == "Done"
    assert out["priority"] == "High"
    assert out["assignee"] == "Sam"
    assert out["sha"] is None and out["files"] == []


def test_missing_fields_degrade_to_empty_strings_not_none():
    out = serialize_event({"occurred_at": T_AWARE})
    assert out["event_type"] == "" and out["title"] == "" and out["workspace"] == ""
    assert out["sha"] is None and out["files"] == []


def test_title_fallback_only_applies_when_requested():
    e = {"source": "teams_subscription", "event_type": "message",
         "title": "", "occurred_at": T_AWARE}
    assert serialize_event(e)["title"] == ""
    assert serialize_event(e, title_fallback=True)["title"] == "message"
    assert serialize_event({**e, "title": "standup"}, title_fallback=True)["title"] == "standup"
