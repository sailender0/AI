"""Pins tool_active_minutes — real per-tool active time = overlap of each tool's
detection windows (ai_tool_events) with the focus blocks (true non-idle time)."""
from datetime import datetime, timedelta, timezone

from app.services.device_analytics import (
    merge_hourly, session_token_totals, tool_active_minutes, tool_active_periods,
)

T0 = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def _block(start_min, end_min):
    return {"start": _at(start_min), "end": _at(end_min), "duration_min": end_min - start_min}


def test_no_focus_blocks_yields_nothing():
    docs = [{"timestamp": _at(0), "tools": ["cursor-ai"]}]
    assert tool_active_minutes(docs, []) == {}


def test_closed_and_open_intervals():
    docs = [
        {"timestamp": _at(0),  "tools": ["cursor-ai"]},
        {"timestamp": _at(30), "tools": ["cursor-ai", "claude-code"]},
        {"timestamp": _at(50), "tools": ["claude-code"]},
    ]
    blocks = [_block(0, 60)]
    out = tool_active_minutes(docs, blocks)
    assert out["cursor-ai"] == 50
    assert out["claude-code"] == 30


def test_idle_time_is_excluded():
    docs = [{"timestamp": _at(0), "tools": ["cursor-ai"]}]
    blocks = [_block(0, 10)]
    assert tool_active_minutes(docs, blocks)["cursor-ai"] == 10


def test_presence_outside_focus_counts_zero():
    docs = [{"timestamp": _at(120), "tools": ["ollama"]}]
    blocks = [_block(0, 30)]
    assert tool_active_minutes(docs, blocks).get("ollama", 0) == 0


def test_periods_split_around_idle():
    docs = [{"timestamp": _at(0), "tools": ["ollama"]}]
    blocks = [_block(0, 20), _block(40, 60)]
    ps = tool_active_periods(docs, blocks)["ollama"]
    assert len(ps) == 2
    assert [p["minutes"] for p in ps] == [20, 20]


def test_periods_merge_contiguous_blocks():
    docs = [{"timestamp": _at(0), "tools": ["ollama"]}]
    blocks = [_block(0, 20), _block(20, 40)]
    ps = tool_active_periods(docs, blocks)["ollama"]
    assert len(ps) == 1 and ps[0]["minutes"] == 40


def test_periods_sum_matches_minutes():
    docs = [
        {"timestamp": _at(0),  "tools": ["cursor-ai"]},
        {"timestamp": _at(30), "tools": ["cursor-ai", "claude-code"]},
        {"timestamp": _at(50), "tools": ["claude-code"]},
    ]
    blocks = [_block(0, 60)]
    mins = tool_active_minutes(docs, blocks)
    pers = tool_active_periods(docs, blocks)
    for tool, total in mins.items():
        assert sum(p["minutes"] for p in pers[tool]) == total


def test_session_tokens_attributed_and_reconcile():
    day_start = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
    periods = [
        {"start": "2026-07-02T09:00:00Z", "end": "2026-07-02T10:00:00Z", "minutes": 60},
        {"start": "2026-07-02T13:00:00Z", "end": "2026-07-02T15:00:00Z", "minutes": 120},
    ]
    hourly = [
        {"hour": 9,  "input_tokens": 100, "output_tokens": 20},
        {"hour": 13, "input_tokens": 200, "output_tokens": 40},
        {"hour": 14, "input_tokens": 300, "output_tokens": 60},
        {"hour": 20, "input_tokens": 90,  "output_tokens": 10},
    ]
    out = session_token_totals(periods, hourly, day_start)
    assert out[0] == {"input_tokens": 100, "output_tokens": 20}
    assert out[1] == {"input_tokens": 590, "output_tokens": 110}
    assert sum(o["input_tokens"] for o in out) == 690


def test_merge_hourly_sums_across_docs():
    docs = [
        {"hourly": [{"hour": 9, "input_tokens": 10, "output_tokens": 1}]},
        {"hourly": [{"hour": 9, "input_tokens": 5,  "output_tokens": 2},
                    {"hour": 10, "input_tokens": 7, "output_tokens": 3}]},
    ]
    assert merge_hourly(docs) == [
        {"hour": 9,  "input_tokens": 15, "output_tokens": 3},
        {"hour": 10, "input_tokens": 7,  "output_tokens": 3},
    ]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
