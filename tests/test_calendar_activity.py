"""Activity calendar service — source mapping, local-day bucketing, timeline shape."""
from datetime import datetime, timezone

import pytest

from app.services.calendar_activity import (
    build_day,
    build_month,
    day_bounds,
    month_bounds,
    row_type,
)

PROFILE = "11111111-1111-1111-1111-111111111111"


def _evt(source="teams_chat", when="2026-07-28T09:00:00+00:00", title="User B",
         user_id="oid-userb", from_self=False, event_type="chat_message", workspace="oneOnOne"):
    return {
        "profile_id": PROFILE, "source": source, "event_type": event_type,
        "occurred_at": datetime.fromisoformat(when), "title": title, "workspace": workspace,
        "raw_payload": {"user_id": user_id, "from_self": from_self, "chat_type": workspace},
    }


class _Cursor:
    """Minimal async cursor over a list, with the .sort() the service chains."""

    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        self._docs = sorted(self._docs, key=lambda d: d[key], reverse=direction < 0)
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


def _col(docs):
    class C:
        def find(self, q):
            return _Cursor(list(docs))
    return C()


def test_chat_and_meeting_and_call_map_to_row_types():
    assert row_type(_evt(source="teams_chat")) == "chat"
    assert row_type(_evt(source="outlook_calendar")) == "meeting"
    assert row_type(_evt(source="teams_call")) == "call"


def test_mail_direction_comes_from_the_event_type():
    assert row_type(_evt(source="outlook_mail", event_type="mail_sent")) == "sent"
    assert row_type(_evt(source="outlook_mail", event_type="mail_received")) == "received"


def test_unrelated_sources_are_ignored():
    """github/jira/gitlab share the collection and must never reach the calendar.
    teams_subscription is excluded too — its direction is unresolved."""
    for src in ("github", "jira", "gitlab", "teams_subscription"):
        assert row_type(_evt(source=src)) is None


def test_month_bounds_are_local_and_half_open():
    start, end = month_bounds("2026-07", "Asia/Kolkata")
    assert start == datetime(2026, 6, 30, 18, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 31, 18, 30, tzinfo=timezone.utc)


def test_month_bounds_roll_over_the_year():
    _, end = month_bounds("2026-12", "UTC")
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_day_bounds_span_one_local_day():
    start, end = day_bounds("2026-07-28", "UTC")
    assert (end - start).total_seconds() == 86400


@pytest.mark.asyncio
async def test_month_counts_dots_per_day_and_collects_people(monkeypatch):
    docs = [
        _evt(when="2026-07-28T09:00:00+00:00"),
        _evt(when="2026-07-28T17:00:00+00:00"),
        _evt(when="2026-07-29T10:00:00+00:00", title="Priya Nair", user_id="oid-priya"),
        _evt(source="github", when="2026-07-28T11:00:00+00:00"),
    ]
    monkeypatch.setattr("app.services.calendar_activity.activity_events", lambda: _col(docs))
    out = await build_month(PROFILE, "2026-07", "UTC")

    assert out["days"]["2026-07-28"] == {"chat": 2}
    assert out["days"]["2026-07-29"] == {"chat": 1}
    assert out["totals"]["chat"] == 3
    assert [p["name"] for p in out["people"]] == ["Priya Nair", "User B"]


@pytest.mark.asyncio
async def test_month_buckets_by_local_day_not_utc(monkeypatch):
    """23:30 UTC on the 28th is 05:00 on the 29th in IST — it belongs to the 29th."""
    docs = [_evt(when="2026-07-28T23:30:00+00:00")]
    monkeypatch.setattr("app.services.calendar_activity.activity_events", lambda: _col(docs))
    out = await build_month(PROFILE, "2026-07", "Asia/Kolkata")
    assert list(out["days"]) == ["2026-07-29"]


@pytest.mark.asyncio
async def test_day_returns_one_row_per_message_oldest_first(monkeypatch):
    docs = [
        _evt(when="2026-07-28T17:00:00+00:00"),
        _evt(when="2026-07-28T09:00:00+00:00"),
    ]
    monkeypatch.setattr("app.services.calendar_activity.activity_events", lambda: _col(docs))
    out = await build_day(PROFILE, "2026-07-28", "UTC")

    assert [i["time"] for i in out["items"]] == ["09:00", "17:00"]
    assert all(i["title"] == "User B" for i in out["items"])


@pytest.mark.asyncio
async def test_day_marks_your_own_messages(monkeypatch):
    docs = [_evt(from_self=True)]
    monkeypatch.setattr("app.services.calendar_activity.activity_events", lambda: _col(docs))
    out = await build_day(PROFILE, "2026-07-28", "UTC")
    assert out["items"][0]["from_self"] is True


@pytest.mark.asyncio
async def test_day_carries_no_message_content(monkeypatch):
    """The timeline exposes a name, a time and the chat type — nothing else."""
    docs = [_evt()]
    monkeypatch.setattr("app.services.calendar_activity.activity_events", lambda: _col(docs))
    out = await build_day(PROFILE, "2026-07-28", "UTC")
    assert set(out["items"][0]) == {"type", "time", "title", "person_id", "from_self", "context"}


@pytest.mark.asyncio
async def test_empty_month_renders_rather_than_failing(monkeypatch):
    """Before any connector runs the page must still load — zero rows, not a 500."""
    monkeypatch.setattr("app.services.calendar_activity.activity_events", lambda: _col([]))
    out = await build_month(PROFILE, "2026-07", "UTC")
    assert out["days"] == {} and out["people"] == []
    assert out["totals"] == {"received": 0, "sent": 0, "chat": 0, "meeting": 0, "call": 0}
