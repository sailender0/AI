"""Pins _tool_active_minutes — real per-tool active time = overlap of each tool's
detection windows (ai_tool_events) with the focus blocks (true non-idle time)."""
from datetime import datetime, timedelta, timezone

from app.routes.agent.analytics import _tool_active_minutes

T0 = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def _block(start_min, end_min):
    return {"start": _at(start_min), "end": _at(end_min), "duration_min": end_min - start_min}


def test_no_focus_blocks_yields_nothing():
    docs = [{"timestamp": _at(0), "tools": ["cursor-ai"]}]
    assert _tool_active_minutes(docs, []) == {}


def test_closed_and_open_intervals():
    # cursor present 09:00, joined by claude 09:30, cursor closes at 09:50.
    # Focus block covers the whole hour 09:00–10:00.
    docs = [
        {"timestamp": _at(0),  "tools": ["cursor-ai"]},
        {"timestamp": _at(30), "tools": ["cursor-ai", "claude-code"]},
        {"timestamp": _at(50), "tools": ["claude-code"]},   # cursor disappeared
    ]
    blocks = [_block(0, 60)]
    out = _tool_active_minutes(docs, blocks)
    assert out["cursor-ai"] == 50      # 09:00 → 09:50
    assert out["claude-code"] == 30    # 09:30 → open, capped at last block end 10:00


def test_idle_time_is_excluded():
    # tool "present" the whole hour, but the user was only active 09:00–09:10.
    docs = [{"timestamp": _at(0), "tools": ["cursor-ai"]}]
    blocks = [_block(0, 10)]
    assert _tool_active_minutes(docs, blocks)["cursor-ai"] == 10


def test_presence_outside_focus_counts_zero():
    # tool detected at 11:00 but the only focus block was 09:00–09:30 → no overlap.
    docs = [{"timestamp": _at(120), "tools": ["ollama"]}]
    blocks = [_block(0, 30)]
    assert _tool_active_minutes(docs, blocks).get("ollama", 0) == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
