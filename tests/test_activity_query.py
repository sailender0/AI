"""Week windowing + percent-change — the tz-sensitive helpers every stats page
rides on (docs/adr-0001-timezone.md). `now` is frozen so exact UTC bounds can be
asserted; only activity_query.datetime is patched (timedelta/timezone stay real)."""
from datetime import datetime, timedelta, timezone

from app.services import activity_query as aq

UTC = timezone.utc


def _freeze(monkeypatch, base_utc: datetime):
    class _F(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return base_utc.astimezone(tzinfo) if tzinfo else base_utc.astimezone(UTC)
    monkeypatch.setattr(aq, "datetime", _F)


def test_week_bounds_utc_this_and_last(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 8, 15, 0, tzinfo=UTC))
    s, e = aq.week_bounds(0, "UTC")
    assert s == datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    assert e == datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    s1, e1 = aq.week_bounds(1, "UTC")
    assert s1 == datetime(2026, 6, 29, 0, 0, tzinfo=UTC)
    assert e1 == datetime(2026, 7, 6, 0, 0, tzinfo=UTC)


def test_week_bounds_offset_zone_anchors_local_monday(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 8, 15, 0, tzinfo=UTC))
    s, e = aq.week_bounds(0, "Asia/Kolkata")
    assert s == datetime(2026, 7, 5, 18, 30, tzinfo=UTC)
    assert e == datetime(2026, 7, 12, 18, 30, tzinfo=UTC)


def test_week_bounds_spans_167h_across_spring_forward(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 3, 4, 12, 0, tzinfo=UTC))
    s, e = aq.week_bounds(0, "America/New_York")
    assert (e - s) == timedelta(hours=167)


def test_week_bounds_blank_tz_defaults_utc(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 8, 15, 0, tzinfo=UTC))
    assert aq.week_bounds(0, "") == aq.week_bounds(0, "UTC")


def test_pct():
    assert aq.pct(10, 5) == 100
    assert aq.pct(5, 10) == -50
    assert aq.pct(10, 0) == 100
    assert aq.pct(0, 0) == 0
    assert aq.pct(0, 10) == -100
    assert aq.pct(3, 7) == -57
