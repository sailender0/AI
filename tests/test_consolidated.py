"""Consolidated report service — range validation, bucketing, and the two gates
that decide what the report may contain. The Mongo collection and the LLM are
mocked; the bucket maths is pure and tested directly."""
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routes.reports import _SOURCE_MAP, _expand, permitted_chips
from app.services.consolidated import (
    SUMMARY_RULES, bucket_mode, bucket_of, build_consolidated, roll_up,
    summarize_consolidated,
)
from app.services.export_pdf import generate_consolidated_pdf


class _AsyncIter:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for i in self._items:
                yield i
        return gen()


def _group(day, source, n):
    return {"_id": {"day": day, "source": source}, "n": n}


def _col(groups=None, sample=None):
    col = MagicMock()
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


# ── chip → source expansion, and the connector gate ────────────────────────────

def test_chips_expand_to_graph_sources_and_never_the_legacy_one():
    assert _expand(["teams"]) == ["teams_chat", "teams_call"]
    assert _expand(["outlook"]) == ["outlook_mail", "outlook_calendar"]
    assert _expand(["teams_subscription", "bogus"]) == []       # retired chip dropped
    assert "teams_subscription" not in _expand(_SOURCE_MAP)     # nor in the all-connectors default


def test_permitted_chips_is_the_connector_gate():
    base = permitted_chips(["consolidated_report"])
    assert base == ["github", "gitlab", "jira"]                 # no comms without a grant
    assert permitted_chips(["teams_activity"]) == ["github", "gitlab", "jira", "teams"]
    assert permitted_chips(["teams_activity", "outlook_activity"]) == list(_SOURCE_MAP)


# ── bucketing (pure) ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("span,mode", [
    (1, "day"), (7, "day"), (14, "day"),
    (15, "week"), (30, "week"), (70, "week"),
    (71, "month"), (366, "month"),
])
def test_bucket_mode_scales_with_the_span(span, mode):
    assert bucket_mode(span) == mode


def test_bucket_labels_carry_their_dates():
    assert bucket_of(date(2026, 8, 4), "day") == ("2026-08-04", "Tue 4 Aug")
    key, label = bucket_of(date(2026, 8, 6), "week")     # a Thursday
    assert key == "2026-W32"
    assert label == "Wk 32 · 3 Aug – 9 Aug"              # Monday-anchored, dates included
    assert bucket_of(date(2026, 8, 4), "month") == ("2026-08", "August 2026")


def test_roll_up_emits_empty_buckets_too():
    per_day = {("2026-08-03", "github"): 5, ("2026-08-05", "jira"): 2}
    rows = roll_up(date(2026, 8, 1), date(2026, 8, 7), "day", per_day, None)
    assert len(rows) == 7                                # a quiet Saturday still gets a row
    assert [r["total"] for r in rows] == [0, 0, 5, 0, 2, 0, 0]
    assert "device_minutes" not in rows[0]               # absent unless device was asked for


def test_roll_up_merges_days_into_one_week():
    per_day = {("2026-08-03", "github"): 5, ("2026-08-06", "github"): 3,
               ("2026-08-06", "jira"): 1}
    rows = roll_up(date(2026, 8, 3), date(2026, 8, 9), "week", per_day, {"2026-08-03": 45})
    assert len(rows) == 1
    assert rows[0]["counts"] == {"github": 8, "jira": 1}
    assert rows[0]["total"] == 9
    assert rows[0]["device_minutes"] == 45


# ── range validation ───────────────────────────────────────────────────────────

async def test_end_before_start_rejected():
    with patch("app.services.consolidated.activity_events", return_value=_col()):
        with pytest.raises(ValueError):
            await build_consolidated(str(uuid.uuid4()), "2026-07-22", "2026-07-20", [], [], _db())


async def test_range_too_large_rejected():
    with patch("app.services.consolidated.activity_events", return_value=_col()):
        with pytest.raises(ValueError):
            await build_consolidated(str(uuid.uuid4()), "2025-01-01", "2026-12-31", [], [], _db())


# ── build ──────────────────────────────────────────────────────────────────────

async def test_build_buckets_by_day_and_totals_per_source():
    col = _col(groups=[_group("2026-07-01", "github", 200),
                       _group("2026-07-02", "teams_chat", 30),
                       _group("2026-07-02", "outlook_mail", 20)])
    with patch("app.services.consolidated.activity_events", return_value=col):
        data = await build_consolidated(str(uuid.uuid4()), "2026-07-01", "2026-07-03", [], [], _db())
    assert data["total"] == 250
    assert data["bucket"] == "day"
    assert data["by_source"] == {"github": 200, "teams_chat": 30, "outlook_mail": 20}
    assert [b["total"] for b in data["buckets"]] == [200, 50, 0]


async def test_counts_only_never_reads_the_events():
    """The depth gate has to stop the read, not just the render — otherwise the
    events still reach the model."""
    col = _col(groups=[_group("2026-07-01", "github", 5)])
    with patch("app.services.consolidated.activity_events", return_value=col):
        data = await build_consolidated(str(uuid.uuid4()), "2026-07-01", "2026-07-03",
                                        [], [], _db(), detail=False)
    col.find.assert_not_called()
    assert data["sample"] == []
    assert data["detail"] is False


async def test_detail_fetches_the_sample_and_flags_truncation():
    col = _col(groups=[_group("2026-07-01", "github", 250)],
               sample=[{"occurred_at": None, "source": "github", "event_type": "commit", "title": "x"}])
    with patch("app.services.consolidated.activity_events", return_value=col):
        data = await build_consolidated(str(uuid.uuid4()), "2026-07-01", "2026-07-03",
                                        [], [], _db(), detail=True)
    col.find.assert_called_once()
    assert data["truncated"] is True                 # > SAMPLE_CAP
    assert len(data["sample"]) == 1


# ── summary ────────────────────────────────────────────────────────────────────

async def test_summary_empty_when_no_activity():
    assert await summarize_consolidated({"total": 0, "detail": True}, "brief", None) == ""


async def test_summary_empty_for_a_counts_only_viewer():
    data = {"total": 9, "detail": False, "by_source": {"github": 9}, "buckets": []}
    with patch("app.services.consolidated.llm.answer", new=AsyncMock()) as ans:
        assert await summarize_consolidated(data, "brief", None) == ""
    ans.assert_not_awaited()                         # the model is never called at all


async def test_summary_prompt_carries_the_writing_rules():
    """The two rules that matter are correctness, not taste: no judgement about the
    person, and no markdown (it would print as literal asterisks in the PDF)."""
    data = {"total": 5, "by_source": {"github": 5}, "start": "a", "end": "b",
            "bucket": "day", "detail": True, "sample": [], "truncated": False, "buckets": []}
    with patch("app.services.consolidated.llm.answer", new=AsyncMock(return_value="ok")) as ans:
        await summarize_consolidated(data, "brief", None)
    prompt = ans.call_args.args[1]
    assert SUMMARY_RULES in prompt
    assert "Never guess intent, quality, effort or productivity." in prompt
    assert "No markdown" in prompt
    assert "'- '" in prompt                           # brief shape reached the model


async def test_summary_detail_uses_more_tokens_and_carries_the_buckets():
    data = {"total": 5, "by_source": {"github": 5}, "start": "a", "end": "b",
            "bucket": "day", "detail": True, "sample": [], "truncated": False,
            "buckets": [{"label": "Mon 3 Aug", "total": 5}]}
    with patch("app.services.consolidated.llm.answer", new=AsyncMock(return_value="ok")) as ans:
        await summarize_consolidated(data, "detail", "focus on reviews")
    assert ans.call_args.kwargs["max_tokens"] == 900
    prompt = ans.call_args.args[1]
    assert "focus on reviews" in prompt               # user prompt passed through, bounded
    assert "Mon 3 Aug: 5" in prompt                   # per-bucket totals reach the model


# ── PDF ────────────────────────────────────────────────────────────────────────

def _pdf_args(**over):
    args = dict(
        who="rahul@x.com", start="2026-08-01", end="2026-08-03", bucket="day",
        total=9, by_source={"github": 6, "teams_chat": 3},
        buckets=[{"key": "2026-08-01", "label": "Sat 1 Aug", "counts": {}, "total": 0,
                  "device_minutes": 0},
                 {"key": "2026-08-02", "label": "Sun 2 Aug",
                  "counts": {"github": 6, "teams_chat": 3}, "total": 9, "device_minutes": 340}],
        summary="- Shipped the retry work.", detail=True, device=True, truncated=False,
    )
    args.update(over)
    return args


def test_pdf_renders_a_real_document():
    out = generate_consolidated_pdf(**_pdf_args())
    assert out.startswith(b"%PDF")
    assert len(out) > 1000


def test_pdf_renders_without_device_or_summary():
    """Counts-only is the default shape, so it has to render on the empty path too."""
    out = generate_consolidated_pdf(**_pdf_args(detail=False, device=False, summary=""))
    assert out.startswith(b"%PDF")


def test_pdf_survives_non_latin1_titles():
    """Core PDF fonts are Latin-1; a smart quote or an em dash from a meeting subject
    would otherwise raise deep inside fpdf."""
    out = generate_consolidated_pdf(**_pdf_args(
        who="rahul@x.com", summary="Reviewed “billing” — see BILL-2291. ✓"))
    assert out.startswith(b"%PDF")


def test_pdf_handles_an_empty_range():
    out = generate_consolidated_pdf(**_pdf_args(total=0, by_source={}, buckets=[], summary=""))
    assert out.startswith(b"%PDF")
