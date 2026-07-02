"""Pins the single UTC<->local conversion layer (docs/adr-0001-timezone.md)."""
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app.services import timezone as tz

KOLKATA = ZoneInfo("Asia/Kolkata")      # UTC+5:30, no DST
NEW_YORK = ZoneInfo("America/New_York")  # DST


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


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")
