"""Activity endpoints — HTTP only.

Event reads, per-day aggregations and the wire shape of an event live in
app/services/activity_query.py; these handlers parse the request, call it, and
shape the response.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import require_profile
from app.services.activity_query import (
    count, daily_counts, find_events, get_profile_tz, serialize_event, trend_rows,
    week_bounds, week_source_stats,
)
from app.services.timezone import day_bounds, resolve, today_str
from app.storage.models import Summary
from app.storage.postgres import get_db

router = APIRouter()


_VALID_SOURCES = {"github", "gitlab", "jira", "teams_subscription"}
_CHART_SOURCES = ["github", "jira", "teams_subscription", "gitlab"]
_INVALID_DATE = {"error": "invalid date"}


def _local_midnight_utc(date_str: str, tz: ZoneInfo) -> datetime:
    """Local midnight of a YYYY-MM-DD date, as UTC.

    Same instant as `services.timezone.day_bounds(date_str, tz)[0]` — kept separate
    only because this one parses with fromisoformat and so tolerates a trailing time
    ("2026-07-02T10:00" reads as the 2nd), which day_bounds' strptime rejects. Nothing
    in the app sends that today; if you ever need one fewer date helper, switching to
    day_bounds is safe as long as you're happy for such input to 400 instead.
    """
    d = datetime.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)


@router.get("/api/events/recent")
async def get_recent_events(
    limit: int = Query(default=20, ge=1, le=200),
    source: str = None,
    start_date: str = None,
    end_date: str = None,
    profile_id: str = Depends(require_profile),
    db: AsyncSession = Depends(get_db),
):
    if source and source not in _VALID_SOURCES:
        return JSONResponse({"error": "invalid_source"}, status_code=400)

    start = end = None
    if start_date or end_date:
        tz = ZoneInfo(await get_profile_tz(profile_id, db) or "UTC")
        start = _local_midnight_utc(start_date, tz) if start_date else None
        end   = _local_midnight_utc(end_date, tz) if end_date else None

    events = await find_events(profile_id, start=start, end=end, source=source, limit=limit)
    return JSONResponse({"events": [
        {"source": e.get("source", ""), **serialize_event(e)} for e in events
    ]})


@router.get("/api/stats")
async def get_stats(period: str = "week", profile_id: str = Depends(require_profile),
                    db: AsyncSession = Depends(get_db)):
    tz_name = await get_profile_tz(profile_id, db)

    if period == "today":
        tz = resolve(tz_name)
        tw_s, tw_e = day_bounds(today_str(tz), tz)
    else:
        tw_s, tw_e = week_bounds(0, tz_name)

    commits  = await count(profile_id, "github", "^commit", tw_s, tw_e)
    prs      = await count(profile_id, "github", "^pr_",    tw_s, tw_e)
    issues   = await count(profile_id, "jira",   None,      tw_s, tw_e)
    meetings = await count(profile_id, "teams_subscription", None, tw_s, tw_e)

    activity_count = commits + prs + issues + meetings
    score = min(100, int(activity_count * 3))

    _now_local = datetime.now(ZoneInfo(tz_name or "UTC"))
    _mon = _now_local - timedelta(days=_now_local.weekday())
    labels, counts_data = await daily_counts(profile_id, days=7, tz_name=tz_name, start_date=_mon.strftime("%Y-%m-%d"))

    return JSONResponse({
        "metrics": [
            {"label": "Commits",       "value": commits},
            {"label": "Pull Requests", "value": prs},
            {"label": "Jira Issues",   "value": issues},
            {"label": "Meetings",      "value": meetings},
        ],
        "activity_score": score,
        "chart": {"labels": labels, "data": counts_data},
    })


@router.get("/api/week-stats")
async def get_week_stats(start: str = None, end: str = None,
                         profile_id: str = Depends(require_profile),
                         db: AsyncSession = Depends(get_db)):
    tz = resolve(await get_profile_tz(profile_id, db))
    try:
        start_dt, _ = day_bounds(start, tz)
        _, end_dt   = day_bounds(end, tz)
    except (ValueError, TypeError):
        return JSONResponse(_INVALID_DATE, status_code=400)

    return JSONResponse(await week_source_stats(profile_id, start_dt, end_dt))


@router.get("/api/day-data")
async def get_day_data(date: str = None, profile_id: str = Depends(require_profile),
                       db: AsyncSession = Depends(get_db)):
    tz = resolve(await get_profile_tz(profile_id, db))
    if not date:
        date = today_str(tz)
    try:
        day_start, day_end = day_bounds(date, tz)
    except ValueError:
        return JSONResponse(_INVALID_DATE, status_code=400)

    raw_events = await find_events(profile_id, start=day_start, end=day_end, limit=500)

    source_counts = {"github": 0, "jira": 0, "teams_subscription": 0, "gitlab": 0}
    result_events = []
    for e in raw_events:
        src = e.get("source", "")
        if src in source_counts:
            source_counts[src] += 1
        result_events.append({"source": src, **serialize_event(e)})

    summary_row = (await db.execute(
        select(Summary)
        .where(
            Summary.profile_id == profile_id,
            Summary.period_type == "daily",
            Summary.period_start >= day_start,
            Summary.period_start < day_end,
        )
        .order_by(Summary.period_end.desc()).limit(1)
    )).scalar_one_or_none()

    return JSONResponse({
        "events": result_events,
        "source_counts": source_counts,
        "summary": summary_row.content if summary_row else None,
    })


@router.get("/api/week-breakdown")
async def get_week_breakdown(start: str = None, end: str = None,
                             profile_id: str = Depends(require_profile),
                             db: AsyncSession = Depends(get_db)):
    tz_name = await get_profile_tz(profile_id, db)
    tz = ZoneInfo(tz_name)

    try:
        start_local = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=tz)
    except (ValueError, TypeError):
        return JSONResponse(_INVALID_DATE, status_code=400)

    today_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        end_local = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=tz)
    except (ValueError, TypeError):
        end_local = today_local
    end_local = min(end_local, today_local)

    days_list = []
    cur = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end_local:
        days_list.append(cur)
        cur += timedelta(days=1)

    if not days_list:
        return JSONResponse({"days": []})

    # Natural order (sort=None): the per-source "first 15" slice below is defined
    # by insertion order, so imposing a sort here would change which items show.
    all_events = await find_events(
        profile_id,
        start=days_list[0].astimezone(timezone.utc),
        end=(days_list[-1] + timedelta(days=1)).astimezone(timezone.utc),
        limit=2000, sort=None,
    )

    for e in all_events:
        ts = e.get("occurred_at")
        if isinstance(ts, datetime) and ts.tzinfo is None:
            e["occurred_at"] = ts.replace(tzinfo=timezone.utc)

    result_days = []
    for day in days_list:
        day_start_utc = day.astimezone(timezone.utc)
        day_end_utc   = (day + timedelta(days=1)).astimezone(timezone.utc)
        day_events = [e for e in all_events if day_start_utc <= e["occurred_at"] < day_end_utc]
        connectors = {}
        for src in _CHART_SOURCES:
            src_events = [e for e in day_events if e.get("source") == src]
            connectors[src] = {
                "count": len(src_events),
                "items": [serialize_event(e, title_fallback=True) for e in src_events[:15]],
            }
        result_days.append({"date": day.strftime("%Y-%m-%d"), "connectors": connectors})

    return JSONResponse({"days": result_days})


def _roll_up_weekly(labels: list[str], pivot: dict) -> dict:
    """Sum a per-source/per-day pivot into weeks, keyed by that week's Monday."""
    week_data: dict = {}
    for day_str in labels:
        d = datetime.strptime(day_str, "%Y-%m-%d").date()
        monday_str = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        week = week_data.setdefault(monday_str, dict.fromkeys(_CHART_SOURCES, 0))
        for src in _CHART_SOURCES:
            week[src] += pivot[src].get(day_str, 0)
    return week_data


@router.get("/api/analytics/trend")
async def get_analytics_trend(
    days: int = Query(default=28, ge=1, le=366),
    group_by: str = "day",
    start_date: str = None,
    profile_id: str = Depends(require_profile),
    db: AsyncSession = Depends(get_db),
):
    tz_name = await get_profile_tz(profile_id, db)
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    if start_date:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        start = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        labels = [(sd + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    else:
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        labels = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]

    results = await trend_rows(profile_id, start, tz_name)

    pivot = {s: dict.fromkeys(labels, 0) for s in _CHART_SOURCES}
    for r in results:
        src = r["_id"]["source"]
        day = r["_id"]["day"]
        if src in pivot and day in pivot[src]:
            pivot[src][day] = r["count"]

    if group_by == "week":
        week_data = _roll_up_weekly(labels, pivot)
        sorted_weeks = sorted(week_data.keys())
        return JSONResponse({
            "labels":     [datetime.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in sorted_weeks],
            "raw_labels": sorted_weeks,
            "sources":    {s: [week_data[w][s] for w in sorted_weeks] for s in _CHART_SOURCES},
            "group_by":   "week",
        })

    event_types: dict = {}
    for r in await trend_rows(profile_id, start, tz_name, by_event_type=True):
        s, d, t = r["_id"]["src"], r["_id"]["day"], r["_id"]["type"]
        if d in labels:
            event_types.setdefault(s, {}).setdefault(d, {})[t] = r["count"]

    return JSONResponse({
        "labels":      [datetime.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in labels],
        "raw_labels":  labels,
        "sources":     {s: list(pivot[s].values()) for s in _CHART_SOURCES},
        "event_types": event_types,
        "group_by":    "day",
    })
