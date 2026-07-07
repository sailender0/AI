"""Date handling for emailed reports: week-snap (Analytics) + future-clamp.
2026-07-06 is a Monday; 07-08 Wed; 07-12 Sun."""
from app.routes.email import _clamp_date, _week_start_of


def test_week_start_snaps_to_monday():
    assert _week_start_of("2026-07-08") == "2026-07-06"   # Wed → Mon
    assert _week_start_of("2026-07-06") == "2026-07-06"   # Mon → Mon
    assert _week_start_of("2026-07-12") == "2026-07-06"   # Sun → Mon


def test_clamp_future_to_today():
    assert _clamp_date("2026-08-01", "2026-07-08") == "2026-07-08"   # future → today
    assert _clamp_date("2026-07-01", "2026-07-08") == "2026-07-01"   # past kept
    assert _clamp_date(None, "2026-07-08") == "2026-07-08"           # default today
    assert _clamp_date("garbage", "2026-07-08") == "2026-07-08"      # malformed → today
