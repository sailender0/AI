"""Attendance report: a people × days grid of activity counts.

Rows are profiles (passed in already role-scoped by the caller — never queried
here), columns are local calendar days, each cell is that user's event count that
day. A day with >= PRESENT_THRESHOLD events counts as "present"; the last column
is the number of present days in the range (attendance, over ALL calendar days
including weekends — holiday awareness waits on the Teams/Outlook calendar work).

The whole grid is one Mongo aggregation grouped by (profile, local-day); rows with
no activity are zero-filled from the profile list, so absent people still appear.
No AI, no sampling — attendance is a fact, not a narrative.
"""
import csv
import io
import logging
from datetime import datetime, timedelta

from app.services.timezone import day_bounds, resolve
from app.storage.mongodb import activity_events

logger = logging.getLogger(__name__)

MAX_SPAN_DAYS = 366          # a year cap keeps the aggregation + grid bounded
PRESENT_THRESHOLD = 3        # >= this many events in a local day = present

_SOURCE_LABEL = {"teams_subscription": "teams"}


async def build_attendance(profiles: list, start: str, end: str, sources: list[str],
                           tz_name: str, threshold: int = PRESENT_THRESHOLD) -> dict:
    """Grid for [start, end] inclusive local days over `profiles`. Raises ValueError
    on a bad or oversized range. `profiles` must already be authorised — this only
    reads activity."""
    tz_name = tz_name or "UTC"
    tz = resolve(tz_name)
    try:
        s_start, _ = day_bounds(start, tz)
        _, e_end = day_bounds(end, tz)
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d1 = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("invalid date")
    if e_end <= s_start or d1 < d0:
        raise ValueError("end date is before start date")
    span = (d1 - d0).days + 1
    if span > MAX_SPAN_DAYS:
        raise ValueError(f"range too large (max {MAX_SPAN_DAYS} days)")

    days = [(d0 + timedelta(days=i)).isoformat() for i in range(span)]
    ids = [str(p.id) for p in profiles]

    counts: dict[tuple[str, str], int] = {}
    if ids:
        q: dict = {"profile_id": {"$in": ids}, "occurred_at": {"$gte": s_start, "$lt": e_end}}
        if sources:
            q["source"] = {"$in": sources}
        async for r in activity_events().aggregate([
            {"$match": q},
            {"$group": {
                "_id": {
                    "p": "$profile_id",
                    "d": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at", "timezone": tz_name}},
                },
                "n": {"$sum": 1},
            }},
        ]):
            counts[(r["_id"]["p"], r["_id"]["d"])] = r["n"]

    rows = []
    for p in profiles:
        pid = str(p.id)
        series = [counts.get((pid, d), 0) for d in days]
        rows.append({
            "profile_id": pid, "email": p.email, "role": p.role,
            "counts": series,
            "present": sum(1 for c in series if c >= threshold),
            "total": sum(series),
        })

    return {
        "start": start, "end": end, "days": days,
        "threshold": threshold,
        "sources": [_SOURCE_LABEL.get(s, s) for s in sources] or ["all"],
        "rows": rows,
    }


def _safe(v) -> str:
    """Neutralise spreadsheet formula injection (emails come from IdP data)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _day_header(iso: str) -> str:
    """"2026-07-20" -> "Jul 20 (Sun)". The parenthesis stops Excel coercing it to a
    date (which renders as ##### in a narrow column) — it stays plain text."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %d (%a)")


def attendance_csv(data: dict) -> str:
    """The grid as CSV — one P/A mark per day (present = >= threshold), plus the
    Days-present total. No raw event counts, no total-events column."""
    th = data.get("threshold", PRESENT_THRESHOLD)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["User", "Role", *[_day_header(d) for d in data["days"]], "Days present"])
    for row in data["rows"]:
        marks = ["P" if c >= th else "A" for c in row["counts"]]
        w.writerow([_safe(row["email"]), _safe(row["role"]), *marks, row["present"]])
    return out.getvalue()
