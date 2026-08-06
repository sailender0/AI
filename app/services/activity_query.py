import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Integration, Profile
from app.storage.mongodb import activity_events


HEARTBEAT_INTERVAL = 30
FOCUS_GAP_SECONDS  = 300


def compute_focus_blocks(heartbeats: list[dict]) -> list[dict]:
    """Group timestamp-sorted, non-idle heartbeats into focus blocks.

    Single source of truth for focus time — used by both the My Activity
    analytics page and the AI activity context so they never disagree.
    Each heartbeat dict needs a "timestamp" (aware datetime). A block's
    duration is end-start, so a lone heartbeat is 0 minutes.
    """
    if not heartbeats:
        return []
    blocks, block_start, block_end = [], None, None
    for hb in heartbeats:
        ts = hb["timestamp"]
        if block_start is None:
            block_start = block_end = ts
        elif (ts - block_end).total_seconds() <= FOCUS_GAP_SECONDS + HEARTBEAT_INTERVAL:
            block_end = ts
        else:
            blocks.append({"start": block_start, "end": block_end,
                           "duration_min": int((block_end - block_start).total_seconds() / 60)})
            block_start = block_end = ts
    if block_start:
        blocks.append({"start": block_start, "end": block_end,
                       "duration_min": int((block_end - block_start).total_seconds() / 60)})
    return blocks


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


def jira_extras(raw: dict) -> dict:
    """Read-time enrichment from the stored payload — webhook payloads nest the
    issue under 'issue'; backfill raw_payload IS the issue. Backfilled docs from
    before the fields widening simply yield Nones (no status in their payload)."""
    issue = raw.get("issue") or raw
    f = issue.get("fields") or {}
    return {
        "issue_key": issue.get("key"),
        "status":    (f.get("status") or {}).get("name"),
        "priority":  (f.get("priority") or {}).get("name"),
        "assignee":  (f.get("assignee") or {}).get("displayName"),
    }


def event_extras(src: str, e: dict, raw: dict) -> dict:
    """Per-source display extras — sha/files for git pushes, issue fields for
    Jira. One implementation shared by the event-serialization endpoints and
    the PDF export."""
    if src == "github":
        sha = (e.get("source_event_id") or raw.get("after") or "")[:7] or None
        head = raw.get("head_commit") or {}
        files = ((head.get("modified") or []) + (head.get("added") or []) + (head.get("removed") or []))[:6]
        return {"sha": sha, "files": files}
    if src == "gitlab":
        commits = raw.get("commits") or []
        if not commits:
            return {"sha": None, "files": []}
        last = commits[-1]
        sha = (last.get("id") or "")[:7] or None
        files = ((last.get("modified") or []) + (last.get("added") or []) + (last.get("removed") or []))[:6]
        return {"sha": sha, "files": files}
    if src == "jira":
        return {"sha": None, "files": [], **jira_extras(raw)}
    return {"sha": None, "files": []}


def iso_utc(ts) -> str:
    """An activity timestamp as an ISO string. Mongo may hand back naive datetimes;
    those are UTC by storage convention, so tag them rather than let them read local."""
    if not isinstance(ts, datetime):
        return str(ts)
    return (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).isoformat()


def serialize_event(e: dict, *, title_fallback: bool = False) -> dict:
    """Wire shape for one activity event, WITHOUT `source` — callers that expose it
    prepend it themselves, because the week-breakdown payload is already grouped by
    source and must not repeat it. `title_fallback` fills an empty title with the
    event type (the week-breakdown list needs something to render)."""
    src   = e.get("source", "")
    title = e.get("title", "")
    return {
        "event_type":  e.get("event_type", ""),
        "title":       (title or e.get("event_type", "")) if title_fallback else title,
        "workspace":   e.get("workspace", ""),
        "occurred_at": iso_utc(e.get("occurred_at")),
        **event_extras(src, e, e.get("raw_payload") or {}),
    }


async def find_events(profile_id: str, *, start: datetime | None = None,
                      end: datetime | None = None, source: str | None = None,
                      limit: int, sort: int | None = -1) -> list[dict]:
    """Activity events for one profile in an optional UTC [start, end) window.
    `sort=None` leaves natural order — the week breakdown relies on it."""
    q: dict = {"profile_id": profile_id}
    if source:
        q["source"] = source
    if start or end:
        window: dict = {}
        if start:
            window["$gte"] = start
        if end:
            window["$lt"] = end
        q["occurred_at"] = window
    cursor = activity_events().find(q)
    if sort is not None:
        cursor = cursor.sort("occurred_at", sort)
    return await cursor.to_list(length=limit)


async def trend_rows(profile_id: str, start: datetime, tz_name: str,
                     by_event_type: bool = False) -> list[dict]:
    """Per-local-day event counts since `start`, grouped by source (and by event
    type when `by_event_type`). Returns the raw aggregation rows; the caller
    pivots them into chart series."""
    group_id: dict = {
        "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at", "timezone": tz_name}},
    }
    group_id.update({"src": "$source", "type": "$event_type"} if by_event_type
                    else {"source": "$source"})
    return await activity_events().aggregate([
        {"$match": {"profile_id": profile_id, "occurred_at": {"$gte": start}}},
        {"$group": {"_id": group_id, "count": {"$sum": 1}}},
    ]).to_list(length=None)


async def week_source_stats(profile_id: str, start: datetime, end: datetime) -> dict:
    """Per-integration counts for one UTC [start, end) window — the Analytics
    week-stats block. Shared by /api/week-stats, the weekly PDF, and email."""
    (gh_commits, gh_prs, gh_issues,
     jira_created, jira_updated, jira_comments,
     teams_msgs, gl_commits, gl_mrs, gl_issues) = await asyncio.gather(
        count(profile_id, "github",             r"^commit",        start, end),
        count(profile_id, "github",             r"^pr_",           start, end),
        count(profile_id, "github",             r"^issue",         start, end),
        count(profile_id, "jira",               "issue_created",   start, end),
        count(profile_id, "jira",               "issue_updated",   start, end),
        count(profile_id, "jira",               "comment",         start, end),
        count(profile_id, "teams_subscription",  None,             start, end),
        count(profile_id, "gitlab",             r"^commit",        start, end),
        count(profile_id, "gitlab",             r"^merge_request", start, end),
        count(profile_id, "gitlab",             r"^issue",         start, end),
    )
    return {
        "github": {"commits": gh_commits,   "pull_requests": gh_prs,   "issues": gh_issues},
        "jira":   {"created": jira_created, "updated": jira_updated,   "comments": jira_comments},
        "teams":  {"messages": teams_msgs},
        "gitlab": {"commits": gl_commits,   "merge_requests": gl_mrs,  "issues": gl_issues},
    }


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
    status_map = {r.source: r.sync_status for r in rows}
    connected = {}
    errors = {}
    for source in ["github", "gitlab", "jira", "teams_subscription"]:
        is_conn = status_map.get(source) in ("active", "error")
        connected[source] = is_conn
        errors[source] = status_map.get(source) == "error"
    return connected, errors
