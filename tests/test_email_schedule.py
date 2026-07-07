"""_digest_due — the per-profile local-time gate for scheduled email digests.
Hour-granular; weekdays skips Sat/Sun; weekly fires only on the chosen weekday."""
from datetime import datetime

from app.routes.email import _digest_due

# 2026-07-06 is a Monday; 07-11 Saturday.
MON_9  = datetime(2026, 7, 6, 9, 0)
MON_10 = datetime(2026, 7, 6, 10, 0)
SAT_9  = datetime(2026, 7, 11, 9, 0)


def test_daily_fires_only_at_hour():
    assert _digest_due("daily", 9, 0, MON_9)
    assert not _digest_due("daily", 9, 0, MON_10)


def test_weekdays_skips_weekend():
    assert _digest_due("weekdays", 9, 0, MON_9)
    assert not _digest_due("weekdays", 9, 0, SAT_9)


def test_weekly_only_on_chosen_weekday():
    assert _digest_due("weekly", 9, 0, MON_9)          # weekday 0 = Monday
    assert not _digest_due("weekly", 9, 5, MON_9)      # wants Saturday
    assert _digest_due("weekly", 9, 5, SAT_9)
