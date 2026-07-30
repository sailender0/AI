"""Consolidated report service — range validation and the AI-summary prompt shape.
The Mongo collection and the LLM are mocked; we assert on aggregation inputs and
that the summariser stays factual/bounded."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.consolidated import build_consolidated, summarize_consolidated


class _AsyncIter:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for i in self._items:
                yield i
        return gen()


def _col(count=0, groups=None, sample=None):
    col = MagicMock()
    col.count_documents = AsyncMock(return_value=count)
    col.aggregate.return_value = _AsyncIter(groups or [])
    cur = MagicMock()
    cur.sort.return_value = cur
    cur.limit.return_value = cur
    cur.to_list = AsyncMock(return_value=sample or [])
    col.find.return_value = cur
    return col


def _db():
    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(timezone="UTC"))
    return db


async def test_end_before_start_rejected():
    with patch("app.services.consolidated.activity_events", return_value=_col()):
        with pytest.raises(ValueError):
            await build_consolidated(str(uuid.uuid4()), "2026-07-22", "2026-07-20", [], [], _db())


async def test_range_too_large_rejected():
    with patch("app.services.consolidated.activity_events", return_value=_col()):
        with pytest.raises(ValueError):
            await build_consolidated(str(uuid.uuid4()), "2025-01-01", "2026-12-31", [], [], _db())


async def test_build_aggregates_counts_and_flags_truncation():
    col = _col(count=250, groups=[{"_id": "github", "n": 200},
                                  {"_id": "teams_subscription", "n": 50}],
               sample=[{"occurred_at": None, "source": "github", "event_type": "commit", "title": "x"}])
    with patch("app.services.consolidated.activity_events", return_value=col):
        data = await build_consolidated(str(uuid.uuid4()), "2026-07-01", "2026-07-20", [], [], _db())
    assert data["total"] == 250
    assert data["truncated"] is True                 # > SAMPLE_CAP
    assert data["by_source"]["github"] == 200
    assert data["by_source"]["teams"] == 50           # teams_subscription normalised


async def test_summary_empty_when_no_activity():
    out = await summarize_consolidated({"total": 0}, "brief", None)
    assert out == ""


async def test_summary_detail_uses_more_tokens():
    data = {"total": 5, "by_source": {"github": 5}, "start": "a", "end": "b",
            "sample": [], "truncated": False}
    with patch("app.services.consolidated.llm.answer", new=AsyncMock(return_value="ok")) as ans:
        await summarize_consolidated(data, "detail", "focus on reviews")
    # detailed → 900-token budget, and the user prompt is passed through (bounded)
    assert ans.call_args.kwargs["max_tokens"] == 900
    assert "focus on reviews" in ans.call_args.args[1]
