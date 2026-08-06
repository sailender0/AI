"""Consolidated report: activity over an arbitrary date range, filtered by
connector, summarised on demand (brief/detail + an optional user prompt).

Unlike the day/week reports this reads a custom [start, end) window, so it
aggregates counts server-side and feeds the AI only a bounded sample — a 3-month
range must never dump every event into the prompt.
"""
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import llm
from app.services.activity_query import get_profile_tz
from app.services.timezone import day_bounds, resolve
from app.storage.mongodb import activity_events

logger = logging.getLogger(__name__)

MAX_SPAN_DAYS = 366
SAMPLE_CAP    = 200


def _norm(source: str) -> str:
    return source or "other"


async def build_consolidated(profile_id: str, start: str, end: str,
                             sources: list[str], event_types: list[str],
                             db: AsyncSession) -> dict:
    """Aggregate activity for [start, end] (inclusive local days). Raises
    ValueError on a bad or oversized range."""
    tz = resolve(await get_profile_tz(profile_id, db))
    try:
        s_start, _ = day_bounds(start, tz)
        _, e_end = day_bounds(end, tz)
    except ValueError:
        raise ValueError("invalid date")
    if e_end <= s_start:
        raise ValueError("end date is before start date")
    if (e_end - s_start).days > MAX_SPAN_DAYS:
        raise ValueError(f"range too large (max {MAX_SPAN_DAYS} days)")

    q: dict = {"profile_id": profile_id, "occurred_at": {"$gte": s_start, "$lt": e_end}}
    if sources:
        q["source"] = {"$in": sources}
    if event_types:
        q["event_type"] = {"$in": event_types}

    col = activity_events()
    total = await col.count_documents(q)

    by_source: dict[str, int] = {}
    async for r in col.aggregate([{"$match": q}, {"$group": {"_id": "$source", "n": {"$sum": 1}}}]):
        by_source[_norm(r["_id"])] = by_source.get(_norm(r["_id"]), 0) + r["n"]

    sample = await col.find(
        q, projection={"_id": 0, "occurred_at": 1, "source": 1, "event_type": 1, "title": 1},
    ).sort("occurred_at", -1).limit(SAMPLE_CAP).to_list(SAMPLE_CAP)

    return {
        "start": start, "end": end,
        "sources": [_norm(s) for s in sources] or ["all"],
        "total": total,
        "by_source": by_source,
        "sample": sample,
        "truncated": total > SAMPLE_CAP,
    }


def _fmt_sample(sample: list[dict]) -> str:
    lines = []
    for e in sample:
        ts = e.get("occurred_at")
        tss = ts.strftime("%Y-%m-%d") if isinstance(ts, datetime) else ""
        title = (e.get("title") or "")[:120]
        lines.append(f"[{tss}] {_norm(e.get('source',''))}/{e.get('event_type','')}: {title}")
    return "\n".join(lines)


async def summarize_consolidated(data: dict, detail: str, user_prompt: str | None) -> str:
    """Narrate the aggregate. detail='detail' → longer, sectioned; else brief."""
    if data["total"] == 0:
        return ""
    is_detail = detail == "detail"
    max_tokens = 900 if is_detail else 300
    style = ("Write a detailed narrative with a short section per connector and "
             "call out notable items." if is_detail
             else "Write a concise brief as a few short bullet points.")
    src_line = ", ".join(f"{k}: {v}" for k, v in sorted(data["by_source"].items())) or "none"

    parts = [
        f"Date range: {data['start']} to {data['end']}.",
        f"Total activity events: {data['total']}. Per connector — {src_line}.",
        style,
    ]
    if user_prompt:
        parts.append(f"Extra instruction from the user: {user_prompt[:500]}")
    parts.append("Activity sample (most recent first"
                 + (", truncated" if data["truncated"] else "") + "):")
    parts.append(_fmt_sample(data["sample"]) or "(no events)")

    system = ("You summarise a developer's work activity across GitHub, GitLab, Jira, "
              "Teams (chats, calls) and Outlook (mail, meetings) over a date range. "
              "Be factual and use only the data given.")
    return await llm.answer(system, "\n\n".join(parts), max_tokens=max_tokens, temperature=0.3)
