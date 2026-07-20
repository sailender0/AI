from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import require_profile
from app.services.activity_query import (
    count, daily_counts, event_extras, get_profile_tz, week_bounds, week_source_stats,
)
from app.services.timezone import day_bounds, resolve, today_str
from app.storage.models import Summary
from app.storage.mongodb import activity_events
from app.storage.postgres import get_db
from sqlalchemy import select

router = APIRouter()


_VALID_SOURCES = {"github", "gitlab", "jira", "teams_subscription"}


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

    q: dict = {"profile_id": profile_id}
    if source:
        q["source"] = source
    if start_date or end_date:
        tz_name = await get_profile_tz(profile_id, db)
        tz = ZoneInfo(tz_name or "UTC")
        time_q: dict = {}
        if start_date:
            s = datetime.fromisoformat(start_date)
            time_q["$gte"] = datetime(s.year, s.month, s.day, tzinfo=tz).astimezone(timezone.utc)
        if end_date:
            e = datetime.fromisoformat(end_date)
            time_q["$lt"] = datetime(e.year, e.month, e.day, tzinfo=tz).astimezone(timezone.utc)
        q["occurred_at"] = time_q

    events = await activity_events().find(q).sort("occurred_at", -1).to_list(length=limit)
    result = []
    for e in events:
        ts  = e.get("occurred_at")
        raw = e.get("raw_payload") or {}
        src = e.get("source", "")
        result.append({
            "source": src,
            "event_type": e.get("event_type", ""),
            "title": e.get("title", ""),
            "workspace": e.get("workspace", ""),
            "occurred_at": (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).isoformat() if isinstance(ts, datetime) else str(ts),
            **event_extras(src, e, raw),
        })
    return JSONResponse({"events": result})


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
        return JSONResponse({"error": "invalid date"}, status_code=400)

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
        return JSONResponse({"error": "invalid date"}, status_code=400)

    raw_events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": day_start, "$lt": day_end}},
    ).sort("occurred_at", -1).to_list(length=500)

    source_counts = {"github": 0, "jira": 0, "teams_subscription": 0, "gitlab": 0}
    result_events = []
    for e in raw_events:
        ts  = e.get("occurred_at")
        src = e.get("source", "")
        raw = e.get("raw_payload") or {}
        if src in source_counts:
            source_counts[src] += 1
        result_events.append({
            "source": src,
            "event_type": e.get("event_type", ""),
            "title": e.get("title", ""),
            "workspace": e.get("workspace", ""),
            "occurred_at": (
                (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).isoformat()
                if isinstance(ts, datetime) else str(ts)
            ),
            **event_extras(src, e, raw),
        })

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
        return JSONResponse({"error": "invalid date"}, status_code=400)

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

    range_start = days_list[0].astimezone(timezone.utc)
    range_end   = (days_list[-1] + timedelta(days=1)).astimezone(timezone.utc)

    all_events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": range_start, "$lt": range_end}},
    ).to_list(length=2000)

    for e in all_events:
        ts = e.get("occurred_at")
        if isinstance(ts, datetime) and ts.tzinfo is None:
            e["occurred_at"] = ts.replace(tzinfo=timezone.utc)

    sources = ["github", "jira", "teams_subscription", "gitlab"]
    result_days = []
    for day in days_list:
        day_start_utc = day.astimezone(timezone.utc)
        day_end_utc   = (day + timedelta(days=1)).astimezone(timezone.utc)
        day_events = [e for e in all_events if day_start_utc <= e["occurred_at"] < day_end_utc]
        connectors = {}
        for src in sources:
            src_events = [e for e in day_events if e.get("source") == src]
            items = []
            for e in src_events[:15]:
                raw = e.get("raw_payload") or {}
                items.append({
                    "event_type":  e.get("event_type", ""),
                    "title":       e.get("title", "") or e.get("event_type", ""),
                    "workspace":   e.get("workspace", ""),
                    "occurred_at": e["occurred_at"].isoformat(),
                    **event_extras(src, e, raw),
                })
            connectors[src] = {"count": len(src_events), "items": items}
        result_days.append({"date": day.strftime("%Y-%m-%d"), "connectors": connectors})

    return JSONResponse({"days": result_days})


@router.get("/api/analytics/trend")
async def get_analytics_trend(
    days: int = 28,
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
    else:
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    sources = ["github", "jira", "teams_subscription", "gitlab"]
    pipeline = [
        {"$match": {"profile_id": profile_id, "occurred_at": {"$gte": start}}},
        {"$group": {
            "_id": {
                "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at", "timezone": tz_name}},
                "source": "$source",
            },
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id.day": 1}},
    ]
    results = await activity_events().aggregate(pipeline).to_list(length=None)

    labels = []
    if start_date:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        for i in range(days):
            labels.append((sd + timedelta(days=i)).strftime("%Y-%m-%d"))
    else:
        for i in range(days - 1, -1, -1):
            d = now - timedelta(days=i)
            labels.append(d.strftime("%Y-%m-%d"))

    pivot = {s: {day: 0 for day in labels} for s in sources}
    for r in results:
        src = r["_id"]["source"]
        day = r["_id"]["day"]
        if src in pivot and day in pivot[src]:
            pivot[src][day] = r["count"]

    if group_by == "week":
        week_data: dict = {}
        for day_str in labels:
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
            monday = d - timedelta(days=d.weekday())
            monday_str = monday.strftime("%Y-%m-%d")
            if monday_str not in week_data:
                week_data[monday_str] = {s: 0 for s in sources}
            for src in sources:
                week_data[monday_str][src] += pivot[src].get(day_str, 0)
        sorted_weeks = sorted(week_data.keys())
        return JSONResponse({
            "labels":     [datetime.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in sorted_weeks],
            "raw_labels": sorted_weeks,
            "sources":    {s: [week_data[w][s] for w in sorted_weeks] for s in sources},
            "group_by":   "week",
        })

    et_pipeline = [
        {"$match": {"profile_id": profile_id, "occurred_at": {"$gte": start}}},
        {"$group": {
            "_id": {
                "day":  {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at", "timezone": tz_name}},
                "src":  "$source",
                "type": "$event_type",
            },
            "count": {"$sum": 1},
        }},
    ]
    et_results = await activity_events().aggregate(et_pipeline).to_list(length=None)
    event_types: dict = {}
    for r in et_results:
        s, d, t = r["_id"]["src"], r["_id"]["day"], r["_id"]["type"]
        if d in labels:
            event_types.setdefault(s, {}).setdefault(d, {})[t] = r["count"]

    display_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in labels]
    return JSONResponse({
        "labels":      display_labels,
        "raw_labels":  labels,
        "sources":     {s: list(pivot[s].values()) for s in sources},
        "event_types": event_types,
        "group_by":    "day",
    })
