"""Pins the single UTC<->local conversion layer (docs/adr-0001-timezone.md)."""
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app.services import timezone as tz

KOLKATA = ZoneInfo("Asia/Kolkata")      # UTC+5:30, no DST
NEW_YORK = ZoneInfo("America/New_York")  # DST
LA = ZoneInfo("America/Los_Angeles")     # UTC-7 (PDT), behind UTC


def test_is_valid_tz():
    assert tz.is_valid_tz("Asia/Kolkata")
    assert tz.is_valid_tz("America/New_York")
    assert not tz.is_valid_tz("Mars/Phobos")
    assert not tz.is_valid_tz("+05:30")
    assert not tz.is_valid_tz(None)
    assert not tz.is_valid_tz("")


def test_resolve_prefers_request_then_profile_then_utc():
    assert tz.resolve("America/New_York", "Asia/Kolkata") == KOLKATA   # request wins
    assert tz.resolve("Asia/Kolkata", None) == KOLKATA                 # profile fallback
    assert tz.resolve(None, None) == ZoneInfo("UTC")                   # final fallback
    assert tz.resolve("Asia/Kolkata", "garbage") == KOLKATA            # invalid request skipped


def test_day_bounds_offset_zone():
    # 2026-07-01 in IST spans UTC 06-30 18:30 -> 07-01 18:30
    start, end = tz.day_bounds("2026-07-01", KOLKATA)
    assert start == datetime(2026, 6, 30, 18, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 1, 18, 30, tzinfo=timezone.utc)
    assert end - start == timedelta(hours=24)


def test_day_bounds_dst_spring_forward_is_23h():
    # DST starts 2026-03-08 in America/New_York -> that local day is 23 hours
    start, end = tz.day_bounds("2026-03-08", NEW_YORK)
    assert end - start == timedelta(hours=23)


def test_day_bounds_dst_fall_back_is_25h():
    # DST ends 2026-11-01 in America/New_York -> that local day is 25 hours
    start, end = tz.day_bounds("2026-11-01", NEW_YORK)
    assert end - start == timedelta(hours=25)


def test_local_date_crosses_utc_midnight():
    # 2026-07-01 20:00 UTC is already 2026-07-02 in IST (+5:30)
    dt = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
    assert tz.local_date(dt, KOLKATA) == "2026-07-02"


def test_local_date_naive_input_assumed_utc():
    naive = datetime(2026, 7, 1, 20, 0)  # Motor-style naive UTC
    assert tz.local_date(naive, KOLKATA) == "2026-07-02"


def test_utc_midnight_crossing_stays_on_local_day():
    """Regression: a moment just after 00:00 UTC is still the PREVIOUS day for a
    behind-UTC zone. The day must roll at local midnight, not UTC midnight — the
    stale-code bug reported the UTC day at 00:01 UTC (5:01pm Pacific)."""
    just_after_utc_midnight = datetime(2026, 7, 3, 0, 1, tzinfo=timezone.utc)  # 5:01pm PDT Jul 2
    assert tz.local_date(just_after_utc_midnight, LA) == "2026-07-02"          # NOT 07-03
    start, end = tz.day_bounds("2026-07-02", LA)
    assert start <= just_after_utc_midnight < end                             # instant is inside the local day
    before_local_day = datetime(2026, 7, 2, 6, 59, tzinfo=timezone.utc)       # 11:59pm PDT Jul 1
    assert before_local_day < start                                           # belongs to the prior local day


def test_today_str_does_not_roll_at_utc_midnight(monkeypatch):
    """today_str must return the LOCAL day at 00:01 UTC, not the UTC day."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            base = datetime(2026, 7, 3, 0, 1, tzinfo=timezone.utc)  # just past UTC midnight
            return base.astimezone(tzinfo) if tzinfo else base
    monkeypatch.setattr(tz, "datetime", _Frozen)
    assert tz.today_str(LA) == "2026-07-02"   # would be 07-03 if it used UTC


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")
