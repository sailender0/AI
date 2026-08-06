"""Report data — the single source for "give me the data behind report X".

Both consumers of report data read from here: the PDF/CSV exports
(app/routes/exports.py) and the email reports (app/routes/email.py). It used to
live inside those route modules, which meant email had to lazy-import three
sibling routes at call time to dodge an import cycle. Nothing in this module
knows about HTTP.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.activity_query import get_profile_tz, week_source_stats
from app.services.device_analytics import build_activity_today, build_activity_week
from app.services.standup import generate as generate_standup
from app.services.timezone import day_bounds, is_date, local_date, resolve, today_str
from app.storage.models import Summary
from app.storage.mongodb import activity_events, local_commits


def clamp_date(date: str | None, today: str) -> str:
    """A valid past-or-today YYYY-MM-DD, else today. Future dates clamp to today
    (string min works: ISO dates sort chronologically)."""
    if is_date(date):
        return min(date, today)
    return today


def week_start_of(date_str: str) -> str:
    """Monday (YYYY-MM-DD) of the week containing date_str — the app's week def."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


async def _week_bounds_utc(profile_id: str, week_start: str, db: AsyncSession):
    """UTC [start, end) for the local week beginning week_start (YYYY-MM-DD)."""
    tz = resolve(await get_profile_tz(profile_id, db))
    ws, _ = day_bounds(week_start, tz)
    last_day = datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)
    _, we = day_bounds(last_day.strftime("%Y-%m-%d"), tz)
    return ws, we


async def fetch_day_events(profile_id: str, date_str: str, db: AsyncSession):
    tz = resolve(await get_profile_tz(profile_id, db))
    try:
        day_start, day_end = day_bounds(date_str, tz)
    except ValueError:
        return [], date_str
    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": day_start, "$lt": day_end}}
    ).sort("occurred_at", 1).to_list(length=500)
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return events, f"{d.strftime('%A, %B')} {d.day} {d.strftime('%Y')}"


async def fetch_week_events(profile_id: str, week_start: str, db: AsyncSession):
    try:
        ws, we = await _week_bounds_utc(profile_id, week_start, db)
    except ValueError:
        return [], week_start
    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": ws, "$lt": we}}
    ).sort("occurred_at", 1).to_list(length=1000)
    d = datetime.strptime(week_start, "%Y-%m-%d")
    d_end = d + timedelta(days=6)
    return events, f"{d.strftime('%b')} {d.day} - {d_end.strftime('%b')} {d_end.day}, {d_end.year}"


async def fetch_week_stats(profile_id: str, week_start: str, db: AsyncSession) -> dict:
    try:
        ws, we = await _week_bounds_utc(profile_id, week_start, db)
    except ValueError:
        return {}
    return await week_source_stats(profile_id, ws, we)


async def get_summary(profile_id: str, period_type: str, date_str: str, db: AsyncSession) -> str:
    tz = resolve(await get_profile_tz(profile_id, db))
    try:
        ref, _ = day_bounds(date_str, tz)
    except ValueError:
        return ""
    window = timedelta(days=7 if period_type == "weekly" else 1)
    row = (await db.execute(
        select(Summary)
        .where(Summary.profile_id == profile_id, Summary.period_type == period_type,
               Summary.period_start >= ref, Summary.period_start < ref + window)
        .order_by(Summary.period_end.desc()).limit(1)
    )).scalar_one_or_none()
    return row.content if row else ""


async def _device_activity_week(profile_id: str, tzinfo, the_date: str, db: AsyncSession) -> dict:
    week_start = week_start_of(the_date)
    week = await build_activity_week(profile_id, tzinfo, week_start)
    w_start, _ = day_bounds(week_start, tzinfo)
    cdocs = await local_commits().find(
        {"profile_id": profile_id,
         "timestamp": {"$gte": w_start, "$lt": w_start + timedelta(days=7)}},
        projection={"timestamp": 1, "_id": 0},
    ).to_list(2000)
    by_day: dict[str, int] = {}
    for c in cdocs:
        ts = c.get("timestamp")
        if ts:
            dk = local_date(ts, tzinfo)
            by_day[dk] = by_day.get(dk, 0) + 1
    week["commits_by_day"] = by_day
    return week


async def _analytics(profile_id: str, tzinfo, the_date: str, db: AsyncSession) -> dict:
    week_start = week_start_of(the_date)
    events, _ = await fetch_week_events(profile_id, week_start, db)
    return {
        "week_start": week_start,
        "stats": await fetch_week_stats(profile_id, week_start, db),
        "summary": await get_summary(profile_id, "weekly", week_start, db),
        "events": events,
    }


async def _my_day(profile_id: str, tzinfo, the_date: str, db: AsyncSession) -> dict:
    events, _ = await fetch_day_events(profile_id, the_date, db)
    counts: dict[str, int] = {}
    for e in events:
        s = e.get("source", "other")
        if s == "teams_subscription":
            s = "teams"
        counts[s] = counts.get(s, 0) + 1
    return {
        "date": the_date,
        "summary": await get_summary(profile_id, "daily", the_date, db),
        "events": events,
        "counts": counts,
    }


async def _standup(profile_id: str, tzinfo, the_date: str, db: AsyncSession) -> dict:
    return await generate_standup(profile_id, db, target_date=the_date)


async def _device_activity(profile_id: str, tzinfo, the_date: str, db: AsyncSession) -> dict:
    data = await build_activity_today(profile_id, tzinfo, the_date)
    data["_date"] = the_date
    return data


_BUILDERS = {
    "standup":              _standup,
    "device_activity":      _device_activity,
    "device_activity_week": _device_activity_week,
    "analytics":            _analytics,
    "my_day":               _my_day,
}

SUPPORTED_KINDS = frozenset(_BUILDERS)


async def fetch_report(kind: str, profile_id: str, db: AsyncSession,
                       date: str | None = None) -> dict:
    """The data behind one report kind, for `profile_id` on `date` (local, default
    today; a future date clamps to today). Raises ValueError for an unknown kind."""
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"unsupported kind: {kind}")
    tzinfo = resolve(await get_profile_tz(profile_id, db))
    return await builder(profile_id, tzinfo, clamp_date(date, today_str(tzinfo)), db)
