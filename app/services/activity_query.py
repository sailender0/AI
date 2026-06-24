from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Integration, LinkedIdentity, Profile
from app.storage.mongodb import activity_events


def week_bounds(weeks_ago: int = 0, tz_name: str = "UTC"):
    tz = ZoneInfo(tz_name or "UTC")
    now = datetime.now(tz)
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    start = monday - timedelta(weeks=weeks_ago)
    end = start + timedelta(weeks=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def get_profile_tz(profile_id: str, db: AsyncSession) -> str:
    profile = await db.get(Profile, profile_id)
    return (profile.timezone or "UTC") if profile else "UTC"


def pct(current: int, previous: int) -> int:
    if previous == 0:
        return 100 if current > 0 else 0
    return round((current - previous) / previous * 100)


async def count(profile_id, source=None, event_type_regex=None, start=None, end=None):
    col = activity_events()
    q = {"profile_id": profile_id}
    if source:
        q["source"] = source
    if event_type_regex:
        q["event_type"] = {"$regex": event_type_regex}
    if start or end:
        q["occurred_at"] = {}
        if start:
            q["occurred_at"]["$gte"] = start
        if end:
            q["occurred_at"]["$lt"] = end
    return await col.count_documents(q)


async def daily_counts(profile_id, source=None, event_type_regex=None, days=7, tz_name: str = "UTC", start_date: str = None):
    tz = ZoneInfo(tz_name or "UTC")
    now = datetime.now(tz)
    if start_date:
        sd    = datetime.strptime(start_date, "%Y-%m-%d")
        start = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        end   = start + timedelta(days=days)
        time_q = {"$gte": start, "$lt": end}
    else:
        start  = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        time_q = {"$gte": start}
    q = {"profile_id": profile_id, "occurred_at": time_q}
    if source:
        q["source"] = source
    if event_type_regex:
        q["event_type"] = {"$regex": event_type_regex}
    pipeline = [
        {"$match": q},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at", "timezone": tz_name}}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    results = await activity_events().aggregate(pipeline).to_list(length=None)
    days_map = {r["_id"]: r["count"] for r in results}
    labels, counts_list = [], []
    if start_date:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
        for i in range(days):
            day = datetime(sd.year, sd.month, sd.day, tzinfo=tz) + timedelta(days=i)
            labels.append(day.strftime("%a %d"))
            counts_list.append(days_map.get(day.strftime("%Y-%m-%d"), 0))
    else:
        for i in range(days - 1, -1, -1):
            day = now - timedelta(days=i)
            labels.append(day.strftime("%a %d"))
            counts_list.append(days_map.get(day.strftime("%Y-%m-%d"), 0))
    return labels, counts_list


async def top_items(profile_id, source, limit=5):
    q = {"profile_id": profile_id, "source": source, "workspace": {"$nin": [None, ""]}}
    pipeline = [
        {"$match": q},
        {"$group": {"_id": "$workspace", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    results = await activity_events().aggregate(pipeline).to_list(length=None)
    return [{"name": r["_id"], "count": r["count"]} for r in results]


async def workspace_breakdown(profile_id, source, event_type_regex=None, days=7, tz_name="UTC", top_n=3, start_date: str = None):
    """Return { day_label: { workspace: count } } for the last `days` days, top_n repos per day."""
    tz    = ZoneInfo(tz_name or "UTC")
    now   = datetime.now(tz)
    if start_date:
        sd    = datetime.strptime(start_date, "%Y-%m-%d")
        start = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
        time_q = {"$gte": start, "$lt": start + timedelta(days=days)}
    else:
        start  = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        time_q = {"$gte": start}
    q: dict = {"profile_id": profile_id, "source": source, "workspace": {"$nin": [None, ""]}, "occurred_at": time_q}
    if event_type_regex:
        q["event_type"] = {"$regex": event_type_regex}
    pipeline = [
        {"$match": q},
        {"$group": {
            "_id": {
                "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at", "timezone": tz_name}},
                "ws":  "$workspace",
            },
            "count": {"$sum": 1},
        }},
    ]
    raw = await activity_events().aggregate(pipeline).to_list(length=None)
    result: dict = {}
    for r in raw:
        day_label = datetime.strptime(r["_id"]["day"], "%Y-%m-%d").strftime("%a %d")
        result.setdefault(day_label, {})[r["_id"]["ws"]] = r["count"]
    return {d: dict(sorted(ws.items(), key=lambda x: -x[1])[:top_n]) for d, ws in result.items()}


async def get_integrations(profile_id: str, db: AsyncSession):
    rows = (await db.execute(select(Integration).where(Integration.profile_id == profile_id))).scalars().all()
    linked = (await db.execute(select(LinkedIdentity).where(LinkedIdentity.profile_id == profile_id))).scalars().all()
    status_map = {r.source: r.sync_status for r in rows}
    linked_providers = {l.provider for l in linked}
    connected = {}
    errors = {}
    for source in ["github", "gitlab", "jira", "teams_subscription"]:
        is_conn = (
            status_map.get(source) in ("active", "error")
            or (source == "github" and "github" in linked_providers)
        )
        connected[source] = is_conn
        errors[source] = status_map.get(source) == "error"
    return connected, errors
