from datetime import date

from app.services.device_analytics import period_ranges


def test_week_ranges():
    r = period_ranges("week", date(2026, 7, 15))
    assert r["this"] == ("2026-07-13", "2026-07-15")
    assert r["last"] == ("2026-07-06", "2026-07-12")


def test_month_ranges():
    r = period_ranges("month", date(2026, 7, 15))
    assert r["this"] == ("2026-07-01", "2026-07-15")
    assert r["last"] == ("2026-06-01", "2026-06-30")


def test_month_ranges_january():
    r = period_ranges("month", date(2026, 1, 10))
    assert r["this"] == ("2026-01-01", "2026-01-10")
    assert r["last"] == ("2025-12-01", "2025-12-31")
