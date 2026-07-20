from datetime import date

from app.ai.query import _resolve_period


def test_named_periods():
    d = date(2026, 7, 15)   # Wednesday
    assert _resolve_period("today", "UTC", d)       == ("2026-07-15", "2026-07-15")
    assert _resolve_period("last_7_days", "UTC", d) == ("2026-07-09", "2026-07-15")
    assert _resolve_period("this_week", "UTC", d)   == ("2026-07-13", "2026-07-15")
    assert _resolve_period("last_week", "UTC", d)   == ("2026-07-06", "2026-07-12")
    assert _resolve_period("this_month", "UTC", d)  == ("2026-07-01", "2026-07-15")
    assert _resolve_period("last_month", "UTC", d)  == ("2026-06-01", "2026-06-30")


def test_unknown_period_defaults_to_this_week():
    assert _resolve_period("garbage", "UTC", date(2026, 7, 15)) == ("2026-07-13", "2026-07-15")
