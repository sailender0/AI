"""Pins _is_scheduled_time — decides, per profile local time, whether the hourly
summary job should generate. Daily at local 23:00, weekly at local Friday 17:00."""
from datetime import datetime

from app.ai.summarizer import _is_scheduled_time

# 2026-07-03 is a Friday; 07-02 Thursday; 07-04 Saturday.
FRI, THU, SAT = datetime(2026, 7, 3, 17, 5), datetime(2026, 7, 2, 17, 5), datetime(2026, 7, 4, 17, 5)


def test_daily_only_at_local_23():
    assert _is_scheduled_time("daily", datetime(2026, 7, 2, 23, 0))
    assert _is_scheduled_time("daily", datetime(2026, 7, 2, 23, 59))
    assert not _is_scheduled_time("daily", datetime(2026, 7, 2, 22, 0))
    assert not _is_scheduled_time("daily", datetime(2026, 7, 2, 0, 0))


def test_weekly_only_at_local_friday_17():
    assert FRI.weekday() == 4                              # sanity: it really is Friday
    assert _is_scheduled_time("weekly", FRI)
    assert not _is_scheduled_time("weekly", datetime(2026, 7, 3, 16, 5))  # right day, wrong hour
    assert not _is_scheduled_time("weekly", THU)           # right hour, wrong day
    assert not _is_scheduled_time("weekly", SAT)


def test_standup_only_at_local_09():
    assert _is_scheduled_time("standup", datetime(2026, 7, 2, 9, 0))
    assert _is_scheduled_time("standup", datetime(2026, 7, 2, 9, 45))
    assert _is_scheduled_time("standup", datetime(2026, 7, 4, 9, 0))   # any weekday
    assert not _is_scheduled_time("standup", datetime(2026, 7, 2, 8, 0))
    assert not _is_scheduled_time("standup", datetime(2026, 7, 2, 10, 0))


def test_unknown_period_always_runs():
    # ad-hoc / manual calls aren't time-gated
    assert _is_scheduled_time("adhoc", datetime(2026, 7, 3, 3, 0))


if __name__ == "__main__":
    for _n, _f in list(globals().items()):
        if _n.startswith("test_"):
            _f()
    print("ok")
