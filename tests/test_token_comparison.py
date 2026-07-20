from datetime import date

from app.routes.agent.analytics import _period_ranges


def test_week_ranges():
    # Wed 2026-07-15 → this week Mon..today, last week prev Mon..Sun
    r = _period_ranges("week", date(2026, 7, 15))
    assert r["this"] == ("2026-07-13", "2026-07-15")
    assert r["last"] == ("2026-07-06", "2026-07-12")


def test_month_ranges():
    # mid-July → this month 1st..today, last month full June
    r = _period_ranges("month", date(2026, 7, 15))
    assert r["this"] == ("2026-07-01", "2026-07-15")
    assert r["last"] == ("2026-06-01", "2026-06-30")


def test_month_ranges_january():
    # crossing the year boundary → last month is Dec of prior year
    r = _period_ranges("month", date(2026, 1, 10))
    assert r["this"] == ("2026-01-01", "2026-01-10")
    assert r["last"] == ("2025-12-01", "2025-12-31")
