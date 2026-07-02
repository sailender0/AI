"""Proves the Ask-AI data fetch selects the correct UTC window for a given
timezone — i.e. it fetches accurately once the tz is known. These are the
functions that build the MongoDB time filter (app/ai/query.py)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.ai.query import _claude_date_range, _intent_to_filter, _scope_to_range

KOLKATA = "Asia/Kolkata"     # UTC+5:30, no DST
NEW_YORK = "America/New_York"  # DST
UTC = timezone.utc


def test_single_local_day_maps_to_utc_window():
    # 2026-07-01 in IST is UTC 06-30 18:30 -> 07-01 18:30
    f = _intent_to_filter({"date_from": "2026-07-01"}, "today", KOLKATA)
    assert f["$gte"] == datetime(2026, 6, 30, 18, 30, tzinfo=UTC)
    assert f["$lte"] == datetime(2026, 7, 1, 18, 30, tzinfo=UTC)


def test_date_range_spans_from_start_to_end_plus_one_day():
    f = _intent_to_filter({"date_from": "2026-07-01", "date_to": "2026-07-03"}, "today", KOLKATA)
    assert f["$gte"] == datetime(2026, 6, 30, 18, 30, tzinfo=UTC)
    assert f["$lte"] == datetime(2026, 7, 3, 18, 30, tzinfo=UTC)  # end day is inclusive


def test_dst_day_is_23_hours():
    # DST starts 2026-03-08 in New York -> that local day is 23h, not 24h
    f = _intent_to_filter({"date_from": "2026-03-08"}, "today", NEW_YORK)
    assert (f["$lte"] - f["$gte"]).total_seconds() == 23 * 3600


def test_invalid_date_falls_back_to_scope():
    # a malformed date must not crash the fetch — it falls back to the scope window
    assert _intent_to_filter({"date_from": "not-a-date"}, "today", KOLKATA) == \
           _scope_to_range("today", KOLKATA)


def test_no_date_uses_scope():
    assert _intent_to_filter({}, "week", KOLKATA) == _scope_to_range("week", KOLKATA)


def test_scope_today_anchors_at_local_midnight():
    start = _scope_to_range("today", KOLKATA)["$gte"].astimezone(ZoneInfo(KOLKATA))
    assert (start.hour, start.minute, start.second) == (0, 0, 0)


def test_scope_week_anchors_at_local_monday_midnight():
    start = _scope_to_range("week", KOLKATA)["$gte"].astimezone(ZoneInfo(KOLKATA))
    assert start.weekday() == 0                      # Monday
    assert (start.hour, start.minute) == (0, 0)


def test_claude_range_single_day_not_over_included():
    # regression: a single-day question must NOT pull in the following day's usage
    tf = _intent_to_filter({"date_from": "2026-07-01"}, "today", KOLKATA)
    assert _claude_date_range(tf, ZoneInfo(KOLKATA)) == ("2026-07-01", "2026-07-01")


def test_claude_range_multi_day():
    tf = _intent_to_filter({"date_from": "2026-07-01", "date_to": "2026-07-03"}, "today", KOLKATA)
    assert _claude_date_range(tf, ZoneInfo(KOLKATA)) == ("2026-07-01", "2026-07-03")


def test_claude_range_no_lower_bound_is_none():
    assert _claude_date_range({}, ZoneInfo(KOLKATA)) is None


if __name__ == "__main__":
    for _n, _f in list(globals().items()):
        if _n.startswith("test_"):
            _f()
    print("ok")
