"""_digest_due — the per-profile local-time gate for scheduled email digests.
Hour-granular; weekdays skips Sat/Sun; weekly fires only on the chosen weekday."""
from datetime import datetime

import pytest

from app.routes.email import _digest_due

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
    assert _digest_due("weekly", 9, 0, MON_9)
    assert not _digest_due("weekly", 9, 5, MON_9)
    assert _digest_due("weekly", 9, 5, SAT_9)


def test_daily_fires_every_day_at_hour():
    for day in range(6, 13):
        assert _digest_due("daily", 9, 0, datetime(2026, 7, day, 9, 0))


@pytest.mark.parametrize("freq", ["daily", "weekdays", "weekly"])
def test_hour_mismatch_never_fires(freq):
    assert not _digest_due(freq, 9, 0, datetime(2026, 7, 6, 8, 0))
    assert not _digest_due(freq, 9, 0, datetime(2026, 7, 6, 10, 0))


@pytest.mark.parametrize("day,expected", [
    (6, True), (7, True), (8, True), (9, True), (10, True),
    (11, False), (12, False),
])
def test_weekdays_matrix(day, expected):
    assert _digest_due("weekdays", 9, 0, datetime(2026, 7, day, 9, 0)) is expected
