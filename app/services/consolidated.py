"""Consolidated report: activity over an arbitrary date range, filtered by
connector, broken into buckets and optionally narrated.

Unlike the day/week reports this reads a custom [start, end) window, so it
aggregates counts server-side and feeds the AI only a bounded sample — a 3-month
range must never dump every event into the prompt.

Two things vary with the caller's permissions, and both are enforced here rather
than hidden in the UI:
  * `sources` is already clamped by the route to the connectors they may see.
  * `detail=False` skips the sample fetch entirely, so a counts-only viewer's
    events never reach the model in the first place.
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import llm
from app.services.activity_query import get_profile_tz
from app.services.timezone import day_bounds, resolve
from app.storage.mongodb import activity_events, device_heartbeats

logger = logging.getLogger(__name__)

MAX_SPAN_DAYS = 366     # a year cap keeps aggregation + AI cost bounded
SAMPLE_CAP    = 200     # most-recent events handed to the model

# Bucket width by span, so any range lands in roughly 3–14 rows. 366 daily rows
# is not a report anyone reads.
DAY_MAX  = 14
WEEK_MAX = 70

_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def bucket_mode(span_days: int) -> str:
    """Row width for a range of `span_days` inclusive local days."""
    if span_days <= DAY_MAX:
        return "day"
    if span_days <= WEEK_MAX:
        return "week"
    return "month"


def bucket_of(day: date, mode: str) -> tuple[str, str]:
    """(sort key, human label) for the bucket `day` falls in. Weeks start Monday
    and carry their dates in the label — "Wk 32" alone sends people to a calendar."""
    if mode == "day":
        return day.isoformat(), f"{day:%a} {day.day} {day:%b}"
    if mode == "week":
        monday = day - timedelta(days=day.weekday())
        sunday = monday + timedelta(days=6)
        iso_year, iso_week, _ = day.isocalendar()
        return (f"{iso_year}-W{iso_week:02d}",
                f"Wk {iso_week} · {monday.day} {monday:%b} – {sunday.day} {sunday:%b}")
    return f"{day.year}-{day.month:02d}", f"{_MONTHS[day.month - 1]} {day.year}"


def _span(start: str, end: str) -> tuple[date, date, int]:
    """Parsed [start, end] with an inclusive day count. Raises ValueError."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    if d1 < d0:
        raise ValueError("end date is before start date")
    days = (d1 - d0).days + 1
    if days > MAX_SPAN_DAYS:
        raise ValueError(f"range too large (max {MAX_SPAN_DAYS} days)")
    return d0, d1, days


def roll_up(d0: date, d1: date, mode: str, per_day: dict[tuple[str, str], int],
            per_day_minutes: dict[str, int] | None) -> list[dict]:
    """Daily (day, source) counts -> one row per bucket, in date order.

    Every bucket in the range is emitted even when empty: a Saturday with no
    activity is a fact, and dropping it would quietly change what a 7-day report
    means. Pure — the Mongo work happens in build_consolidated.
    """
    rows: dict[str, dict] = {}
    day = d0
    while day <= d1:
        key, label = bucket_of(day, mode)
        row = rows.setdefault(key, {"key": key, "label": label, "counts": {},
                                    "total": 0, "device_minutes": 0})
        for (d, source), n in per_day.items():
            if d == day.isoformat():
                row["counts"][source] = row["counts"].get(source, 0) + n
                row["total"] += n
        if per_day_minutes is not None:
            row["device_minutes"] += per_day_minutes.get(day.isoformat(), 0)
        day += timedelta(days=1)

    if per_day_minutes is None:
        for row in rows.values():
            row.pop("device_minutes")
    return [rows[k] for k in sorted(rows)]


async def build_consolidated(profile_id: str, start: str, end: str,
                             sources: list[str], event_types: list[str],
                             db: AsyncSession, *, detail: bool = False,
                             device: bool = False) -> dict:
    """Aggregate activity for [start, end] (inclusive local days) for ONE profile.
    Raises ValueError on a bad or oversized range.

    `detail` adds the event sample (and is what later unlocks the AI summary);
    `device` adds active minutes from the desktop agent's heartbeats.
    """
    tz_name = await get_profile_tz(profile_id, db) or "UTC"
    tz = resolve(tz_name)
    try:
        d0, d1, span = _span(start, end)
        s_start, _ = day_bounds(start, tz)
        _, e_end = day_bounds(end, tz)
    except ValueError as exc:
        raise ValueError(str(exc) if str(exc) != "" else "invalid date") from exc

    q: dict = {"profile_id": profile_id, "occurred_at": {"$gte": s_start, "$lt": e_end}}
    if sources:
        q["source"] = {"$in": sources}
    if event_types:
        q["event_type"] = {"$in": event_types}

    col = activity_events()
    # One pass, grouped by (local day, source). Mongo does the timezone maths so we
    # never pull the events themselves just to bucket them.
    per_day: dict[tuple[str, str], int] = {}
    by_source: dict[str, int] = {}
    total = 0
    async for r in col.aggregate([
        {"$match": q},
        {"$group": {
            "_id": {
                "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at",
                                          "timezone": tz_name}},
                "source": "$source",
            },
            "n": {"$sum": 1},
        }},
    ]):
        day, source = r["_id"]["day"], r["_id"]["source"] or "other"
        per_day[(day, source)] = per_day.get((day, source), 0) + r["n"]
        by_source[source] = by_source.get(source, 0) + r["n"]
        total += r["n"]

    per_day_minutes = await _active_minutes(profile_id, s_start, e_end, tz_name) if device else None

    sample: list[dict] = []
    if detail and total:
        # Counts-only callers never reach this, so their events are not merely
        # hidden from the page — they are never read, and never reach the model.
        sample = await col.find(
            q, projection={"_id": 0, "occurred_at": 1, "source": 1, "event_type": 1, "title": 1},
        ).sort("occurred_at", -1).limit(SAMPLE_CAP).to_list(SAMPLE_CAP)

    mode = bucket_mode(span)
    return {
        "start": start, "end": end,
        "bucket": mode,
        "sources": sources or ["all"],
        "total": total,
        "by_source": by_source,
        "buckets": roll_up(d0, d1, mode, per_day, per_day_minutes),
        "sample": sample,
        "truncated": detail and total > SAMPLE_CAP,
        "detail": detail,
        "device": device,
    }


async def _active_minutes(profile_id: str, s_start: datetime, e_end: datetime,
                          tz_name: str) -> dict[str, int]:
    """Local day -> minutes with at least one non-idle heartbeat.

    ponytail: distinct active minutes, not compute_focus_blocks — blocks bridge
    short gaps, which needs every timestamp and doesn't survive a year-long range.
    Swap in blocks per day if the two numbers ever need to agree exactly.
    """
    out: dict[str, int] = {}
    try:
        async for r in device_heartbeats().aggregate([
            {"$match": {"profile_id": profile_id, "idle": False,
                        "timestamp": {"$gte": s_start, "$lt": e_end}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d %H:%M",
                                                  "date": "$timestamp", "timezone": tz_name}}}},
            {"$group": {"_id": {"$substrBytes": ["$_id", 0, 10]}, "minutes": {"$sum": 1}}},
        ]):
            out[r["_id"]] = r["minutes"]
    except Exception as exc:
        # A missing desktop agent must not fail the whole report.
        logger.warning("device activity unavailable for %s: %s", profile_id, exc)
    return out



SUMMARY_RULES = """How to write it:
1. Use only the data given. Never guess intent, quality, effort or productivity.
2. Do not praise, criticise, rank, or comment on how hard anyone worked — describe
   what the activity was, not what it says about the person.
3. Lead with what the work was about. Do not just restate totals the reader can
   already see in the table.
4. Name concrete things when they appear: ticket keys, repository names, meeting
   subjects, the people someone worked with.
5. Point out anything a reader would want flagged — days with no activity at all,
   an unusual spike, or work that stops partway through the range.
6. If the sample was truncated, say the summary covers only the most recent events.
7. Plain sentences only. No markdown, no headings, no bold, no emoji."""

SHAPE_BRIEF = ("Shape: 3 to 5 single-line bullets, each starting with '- '. "
               "No preamble and no closing line.")

SHAPE_DETAIL = ("Shape: a 2-3 sentence overview, then one short paragraph per "
                "connector that has activity, then a final line starting "
                "'Notable:' listing up to 5 specific items. Separate the parts "
                "with a blank line.")


def _fmt_sample(sample: list[dict]) -> str:
    lines = []
    for e in sample:
        ts = e.get("occurred_at")
        tss = ts.strftime("%Y-%m-%d") if isinstance(ts, datetime) else ""
        title = (e.get("title") or "")[:120]
        lines.append(f"[{tss}] {e.get('source', '')}/{e.get('event_type', '')}: {title}")
    return "\n".join(lines)


async def summarize_consolidated(data: dict, detail: str, user_prompt: str | None) -> str:
    """Narrate the aggregate. detail='detail' → longer, sectioned; else brief.

    Returns "" when there is nothing to say, and when the caller is counts-only —
    a summary without the events would be invention, not a summary.
    """
    if data["total"] == 0 or not data.get("detail"):
        return ""
    is_detail = detail == "detail"
    max_tokens = 900 if is_detail else 300
    src_line = ", ".join(f"{k}: {v}" for k, v in sorted(data["by_source"].items())) or "none"
    per_bucket = "; ".join(f"{b['label']}: {b['total']}" for b in data.get("buckets", []))

    parts = [
        f"Date range: {data['start']} to {data['end']}, grouped by {data['bucket']}.",
        f"Total activity events: {data['total']}. Per connector — {src_line}.",
        f"Per {data['bucket']} — {per_bucket}." if per_bucket else "",
        SUMMARY_RULES,
        SHAPE_DETAIL if is_detail else SHAPE_BRIEF,
    ]
    if user_prompt:
        # Bounded + labelled: it's the user's own data, so injection risk is low,
        # but keep it clearly separated from the instruction above.
        parts.append(f"Extra instruction from the user: {user_prompt[:500]}")
    parts.append("Activity sample (most recent first"
                 + (", truncated" if data["truncated"] else "") + "):")
    parts.append(_fmt_sample(data["sample"]) or "(no events)")

    system = ("You summarise a developer's work activity across GitHub, GitLab, Jira, "
              "Teams (chats, calls) and Outlook (mail, meetings) over a date range. "
              "Be factual and use only the data given.")
    return await llm.answer(system, "\n\n".join(p for p in parts if p),
                            max_tokens=max_tokens, temperature=0.3)
