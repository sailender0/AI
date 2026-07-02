"""Pins compute_focus_blocks — the single source of truth for focus time,
used by the My Activity page, the AI chat context, and the standup builder."""
from datetime import datetime, timedelta, timezone

from app.services.activity_query import compute_focus_blocks


def _hbs(*offsets_seconds):
    """Heartbeats at t0 + each offset (seconds), tz-aware."""
    t0 = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)
    return [{"timestamp": t0 + timedelta(seconds=s)} for s in offsets_seconds]


def test_empty():
    assert compute_focus_blocks([]) == []


def test_single_heartbeat_is_zero_minutes():
    blocks = compute_focus_blocks(_hbs(0))
    assert len(blocks) == 1
    assert blocks[0]["duration_min"] == 0


def test_continuous_run_is_one_block():
    # 30s heartbeats for 20 minutes → one block spanning 19m30s → 19 min (floored)
    blocks = compute_focus_blocks(_hbs(*range(0, 20 * 60, 30)))
    assert len(blocks) == 1
    assert blocks[0]["duration_min"] == 19


def test_gap_over_five_minutes_splits_blocks():
    # two runs separated by a 10-minute idle gap
    first  = list(range(0, 5 * 60, 30))              # 0..4:30
    second = [s + 15 * 60 for s in range(0, 5 * 60, 30)]  # starts 10 min after first ends
    blocks = compute_focus_blocks(_hbs(*first, *second))
    assert len(blocks) == 2


def test_gap_within_threshold_stays_one_block():
    # a 5-minute gap (== FOCUS_GAP_SECONDS, within +HEARTBEAT_INTERVAL tolerance) does not split
    blocks = compute_focus_blocks(_hbs(0, 30, 30 + 300))
    assert len(blocks) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
