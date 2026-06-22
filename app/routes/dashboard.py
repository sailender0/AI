import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.models import Integration, LinkedIdentity, Profile, Summary
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal
from app.storage.redis_client import get_redis
from app.ws_manager import manager as ws_manager

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "jira": "Jira",
    "teams_subscription": "Teams",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _week_bounds(weeks_ago: int = 0, tz_name: str = "UTC"):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name or "UTC")
    now = datetime.now(tz)
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    start = monday - timedelta(weeks=weeks_ago)
    end = start + timedelta(weeks=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def _get_profile_tz(profile_id: str) -> str:
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
        return (profile.timezone or "UTC") if profile else "UTC"


def _pct(current: int, previous: int) -> int:
    if previous == 0:
        return 100 if current > 0 else 0
    return round((current - previous) / previous * 100)


async def _count(profile_id, source=None, event_type_regex=None, start=None, end=None):
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


async def _daily_counts(profile_id, source=None, event_type_regex=None, days=7, tz_name: str = "UTC", start_date: str = None):
    from zoneinfo import ZoneInfo
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
    labels, counts = [], []
    if start_date:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
        for i in range(days):
            day = datetime(sd.year, sd.month, sd.day, tzinfo=tz) + timedelta(days=i)
            labels.append(day.strftime("%a %d"))
            counts.append(days_map.get(day.strftime("%Y-%m-%d"), 0))
    else:
        for i in range(days - 1, -1, -1):
            day = now - timedelta(days=i)
            labels.append(day.strftime("%a %d"))
            counts.append(days_map.get(day.strftime("%Y-%m-%d"), 0))
    return labels, counts


async def _top_items(profile_id, source, limit=5):
    q = {"profile_id": profile_id, "source": source, "workspace": {"$nin": [None, ""]}}
    pipeline = [
        {"$match": q},
        {"$group": {"_id": "$workspace", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    results = await activity_events().aggregate(pipeline).to_list(length=None)
    return [{"name": r["_id"], "count": r["count"]} for r in results]


async def _workspace_breakdown(profile_id, source, event_type_regex=None, days=7, tz_name="UTC", top_n=3, start_date: str = None):
    """Return { day_label: { workspace: count } } for the last `days` days, top_n repos per day."""
    from zoneinfo import ZoneInfo
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


async def _get_integrations(profile_id: str):
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Integration).where(Integration.profile_id == profile_id))).scalars().all()
        linked = (await db.execute(select(LinkedIdentity).where(LinkedIdentity.profile_id == profile_id))).scalars().all()
    connected = {r.source for r in rows if r.sync_status == "active"}
    linked_providers = {l.provider for l in linked}
    result = {}
    for source in ["github", "gitlab", "jira", "teams_subscription"]:
        result[source] = source in connected or (source == "github" and "github" in linked_providers)
    return result


def _greeting(email: str) -> str:
    hour = datetime.now(timezone.utc).hour
    name = email.split("@")[0].split(".")[0].capitalize()
    if hour < 12:
        return f"Good morning, {name}"
    if hour < 17:
        return f"Good afternoon, {name}"
    return f"Good evening, {name}"


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    profile_id = await get_profile_from_session(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "logged_in": profile_id is not None,
            "github_app_slug": settings.GITHUB_APP_SLUG,
            "app_base_url": settings.APP_BASE_URL,
            "active_page": "overview",
        },
    )


@router.get("/github", response_class=HTMLResponse)
async def github_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="github.html", context={"active_page": "github"})


@router.get("/jira", response_class=HTMLResponse)
async def jira_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="jira.html", context={"active_page": "jira"})


@router.get("/teams", response_class=HTMLResponse)
async def teams_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="teams.html", context={"active_page": "teams"})


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="ai.html", context={"active_page": "ai"})


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="help.html", context={"active_page": "help"})


@router.get("/gitlab", response_class=HTMLResponse)
async def gitlab_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="gitlab.html", context={"active_page": "gitlab"})


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="analytics.html", context={"active_page": "analytics"})


@router.get("/my-day", response_class=HTMLResponse)
async def my_day_page(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="my_day.html", context={"active_page": "my_day"})


# ---------------------------------------------------------------------------
# API — shared
# ---------------------------------------------------------------------------

@router.get("/api/me")
async def get_me(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"authenticated": False})
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
        if not profile:
            return JSONResponse({"authenticated": False})
    integrations = await _get_integrations(profile_id)
    return JSONResponse({
        "authenticated": True,
        "email": profile.email,

        "profile_id": str(profile.id),
        "integrations": integrations,
        "connect_urls": {
            "github": f"https://github.com/apps/{settings.GITHUB_APP_SLUG}/installations/new",
            "gitlab": f"{settings.APP_BASE_URL}/connect/gitlab",
            "jira": f"{settings.APP_BASE_URL}/connect/jira",
            "teams_subscription": None,
        },
    })


@router.patch("/api/profile/timezone")
async def update_profile_timezone(request: Request):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    body = await request.json()
    tz_name = body.get("timezone", "UTC")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return JSONResponse({"error": "invalid timezone"}, status_code=400)
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
        if profile:
            profile.timezone = tz_name
            await db.commit()
    return JSONResponse({"ok": True, "timezone": tz_name})


@router.get("/api/events/recent")
async def get_recent_events(
    request: Request,
    limit: int = 20,
    source: str = None,
    start_date: str = None,
    end_date: str = None,
):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    q: dict = {"profile_id": profile_id}
    if source:
        q["source"] = source
    if start_date or end_date:
        from zoneinfo import ZoneInfo
        tz_name = await _get_profile_tz(profile_id)
        tz = ZoneInfo(tz_name or "UTC")
        time_q: dict = {}
        if start_date:
            s = datetime.fromisoformat(start_date)
            time_q["$gte"] = datetime(s.year, s.month, s.day, tzinfo=tz).astimezone(timezone.utc)
        if end_date:
            e = datetime.fromisoformat(end_date)
            time_q["$lt"] = datetime(e.year, e.month, e.day, tzinfo=tz).astimezone(timezone.utc)
        q["occurred_at"] = time_q

    events = (
        await activity_events()
        .find(q)
        .sort("occurred_at", -1)
        .to_list(length=limit)
    )
    result = []
    for e in events:
        ts  = e.get("occurred_at")
        raw = e.get("raw_payload") or {}
        src = e.get("source", "")

        sha   = None
        files = []
        if src == "github":
            raw_sha = e.get("source_event_id") or raw.get("after") or ""
            if raw_sha:
                sha = raw_sha[:7]
            head = raw.get("head_commit") or {}
            files = (head.get("modified") or []) + (head.get("added") or []) + (head.get("removed") or [])
            files = files[:6]
        elif src == "gitlab":
            commits = raw.get("commits") or []
            if commits:
                raw_sha = commits[-1].get("id") or ""
                sha = raw_sha[:7] if raw_sha else None
                files = (
                    (commits[-1].get("modified") or []) +
                    (commits[-1].get("added") or []) +
                    (commits[-1].get("removed") or [])
                )[:6]

        result.append({
            "source": src,
            "event_type": e.get("event_type", ""),
            "title": e.get("title", ""),
            "workspace": e.get("workspace", ""),
            "occurred_at": (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).isoformat() if isinstance(ts, datetime) else str(ts),
            "sha": sha,
            "files": files,
        })
    return JSONResponse({"events": result})


# ---------------------------------------------------------------------------
# API — overview stats
# ---------------------------------------------------------------------------

@router.get("/api/stats")
async def get_stats(request: Request, period: str = "week"):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)

    if period == "today":
        from zoneinfo import ZoneInfo
        user_tz = ZoneInfo(tz_name)
        now_local = datetime.now(user_tz)
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
    else:
        tw_s, tw_e = _week_bounds(0, tz_name)

    commits  = await _count(profile_id, "github", "^commit", tw_s, tw_e)
    prs      = await _count(profile_id, "github", "^pr_",    tw_s, tw_e)
    issues   = await _count(profile_id, "jira",   None,      tw_s, tw_e)
    meetings = await _count(profile_id, "teams_subscription", None, tw_s, tw_e)

    total = commits + prs * 2 + issues * 1.5 + meetings
    score = min(100, int(total * 3))

    from zoneinfo import ZoneInfo as _ZI
    _now_local = datetime.now(_ZI(tz_name or "UTC"))
    _mon = _now_local - timedelta(days=_now_local.weekday())
    _mon_str = _mon.strftime("%Y-%m-%d")
    labels, counts = await _daily_counts(profile_id, days=7, tz_name=tz_name, start_date=_mon_str)

    return JSONResponse({
        "metrics": [
            {"label": "Commits",       "value": commits},
            {"label": "Pull Requests", "value": prs},
            {"label": "Jira Issues",   "value": issues},
            {"label": "Meetings",      "value": meetings},
        ],
        "ai_score": score,
        "chart": {"labels": labels, "data": counts},
    })


# ---------------------------------------------------------------------------
# API — GitHub stats
# ---------------------------------------------------------------------------

@router.get("/api/github/stats")
async def get_github_stats(request: Request, period: str = "week", start_date: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)

    if period == "today":
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        commits  = await _count(profile_id, "github", "^commit",      tw_s, tw_e)
        prs      = await _count(profile_id, "github", "^pr_",         tw_s, tw_e)
        reviews  = await _count(profile_id, "github", "^pr_reviewed", tw_s, tw_e)
        issues   = await _count(profile_id, "github", "^issue",       tw_s, tw_e)
        metrics  = [
            {"label": "Pull Requests", "value": prs},
            {"label": "Commits",       "value": commits},
            {"label": "Reviews",       "value": reviews},
            {"label": "Issues",        "value": issues},
        ]
    else:
        tw_s, tw_e = _week_bounds(0, tz_name)
        lw_s, lw_e = _week_bounds(1, tz_name)
        commits  = await _count(profile_id, "github", "^commit",      tw_s, tw_e)
        prs      = await _count(profile_id, "github", "^pr_",         tw_s, tw_e)
        reviews  = await _count(profile_id, "github", "^pr_reviewed", tw_s, tw_e)
        issues   = await _count(profile_id, "github", "^issue",       tw_s, tw_e)
        metrics  = [
            {"label": "Pull Requests", "value": prs,     "change": _pct(prs,     await _count(profile_id, "github", "^pr_",         lw_s, lw_e))},
            {"label": "Commits",       "value": commits, "change": _pct(commits, await _count(profile_id, "github", "^commit",      lw_s, lw_e))},
            {"label": "Reviews",       "value": reviews, "change": _pct(reviews, await _count(profile_id, "github", "^pr_reviewed", lw_s, lw_e))},
            {"label": "Issues",        "value": issues,  "change": _pct(issues,  await _count(profile_id, "github", "^issue",       lw_s, lw_e))},
        ]

    labels,  commits_daily = await _daily_counts(profile_id, "github", r"^commit",      days=7, tz_name=tz_name, start_date=start_date)
    _,       pr_daily      = await _daily_counts(profile_id, "github", r"^pr_",         days=7, tz_name=tz_name, start_date=start_date)
    _,       issue_daily   = await _daily_counts(profile_id, "github", r"^issue",       days=7, tz_name=tz_name, start_date=start_date)
    _,       review_daily  = await _daily_counts(profile_id, "github", r"^pr_reviewed", days=7, tz_name=tz_name, start_date=start_date)
    top_repos = await _top_items(profile_id, "github")
    repos = {
        "commits":       await _workspace_breakdown(profile_id, "github", r"^commit",      tz_name=tz_name, start_date=start_date),
        "pull_requests": await _workspace_breakdown(profile_id, "github", r"^pr_",         tz_name=tz_name, start_date=start_date),
        "issues":        await _workspace_breakdown(profile_id, "github", r"^issue",       tz_name=tz_name, start_date=start_date),
        "reviews":       await _workspace_breakdown(profile_id, "github", r"^pr_reviewed", tz_name=tz_name, start_date=start_date),
    }

    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": {
                "commits":       commits_daily,
                "pull_requests": pr_daily,
                "issues":        issue_daily,
                "reviews":       review_daily,
            },
            "repos": repos,
        },
        "top_items": top_repos,
        "top_label": "Top Repositories",
    })


# ---------------------------------------------------------------------------
# API — Jira stats
# ---------------------------------------------------------------------------

@router.get("/api/jira/stats")
async def get_jira_stats(request: Request, period: str = "week", start_date: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)

    if period == "today":
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        metrics = [
            {"label": "Created",  "value": await _count(profile_id, "jira", "issue_created", tw_s, tw_e)},
            {"label": "Updated",  "value": await _count(profile_id, "jira", "issue_updated", tw_s, tw_e)},
            {"label": "Comments", "value": await _count(profile_id, "jira", "comment",       tw_s, tw_e)},
            {"label": "Total",    "value": await _count(profile_id, "jira", None,            tw_s, tw_e)},
        ]
    else:
        tw_s, tw_e = _week_bounds(0, tz_name)
        lw_s, lw_e = _week_bounds(1, tz_name)
        created_n  = await _count(profile_id, "jira", "issue_created", tw_s, tw_e)
        created_p  = await _count(profile_id, "jira", "issue_created", lw_s, lw_e)
        updated_n  = await _count(profile_id, "jira", "issue_updated", tw_s, tw_e)
        updated_p  = await _count(profile_id, "jira", "issue_updated", lw_s, lw_e)
        comments_n = await _count(profile_id, "jira", "comment",       tw_s, tw_e)
        comments_p = await _count(profile_id, "jira", "comment",       lw_s, lw_e)
        total_n    = await _count(profile_id, "jira", None,            tw_s, tw_e)
        total_p    = await _count(profile_id, "jira", None,            lw_s, lw_e)
        metrics = [
            {"label": "Created",  "value": created_n,  "change": _pct(created_n,  created_p)},
            {"label": "Updated",  "value": updated_n,  "change": _pct(updated_n,  updated_p)},
            {"label": "Comments", "value": comments_n, "change": _pct(comments_n, comments_p)},
            {"label": "Total",    "value": total_n,    "change": _pct(total_n,    total_p)},
        ]

    labels,  created_daily = await _daily_counts(profile_id, "jira", "issue_created", days=7, tz_name=tz_name, start_date=start_date)
    _,       updated_daily = await _daily_counts(profile_id, "jira", "issue_updated", days=7, tz_name=tz_name, start_date=start_date)
    _,       comment_daily = await _daily_counts(profile_id, "jira", "comment",       days=7, tz_name=tz_name, start_date=start_date)
    top_projects = await _top_items(profile_id, "jira")
    repos = {
        "created":  await _workspace_breakdown(profile_id, "jira", "issue_created", tz_name=tz_name, start_date=start_date),
        "updated":  await _workspace_breakdown(profile_id, "jira", "issue_updated", tz_name=tz_name, start_date=start_date),
        "comments": await _workspace_breakdown(profile_id, "jira", "comment",       tz_name=tz_name, start_date=start_date),
    }

    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": {
                "created":  created_daily,
                "updated":  updated_daily,
                "comments": comment_daily,
            },
            "repos": repos,
        },
        "top_items": top_projects,
        "top_label": "Top Projects",
    })


# ---------------------------------------------------------------------------
# API — Teams stats
# ---------------------------------------------------------------------------

@router.get("/api/teams/stats")
async def get_teams_stats(request: Request, period: str = "week", start_date: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)

    if period == "today":
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        msgs = await _count(profile_id, "teams_subscription", None, tw_s, tw_e)
        metrics = [{"label": "Messages", "value": msgs}]
    else:
        tw_s, tw_e = _week_bounds(0, tz_name)
        lw_s, lw_e = _week_bounds(1, tz_name)
        msgs_now  = await _count(profile_id, "teams_subscription", None, tw_s, tw_e)
        msgs_prev = await _count(profile_id, "teams_subscription", None, lw_s, lw_e)
        metrics = [{"label": "Messages", "value": msgs_now, "change": _pct(msgs_now, msgs_prev)}]

    labels, messages_daily = await _daily_counts(profile_id, "teams_subscription", days=7, tz_name=tz_name, start_date=start_date)
    repos = {
        "messages": await _workspace_breakdown(profile_id, "teams_subscription", tz_name=tz_name, start_date=start_date),
    }

    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": { "messages": messages_daily },
            "repos": repos,
        },
        "top_items": [],
        "top_label": "Top Channels",
    })


# ---------------------------------------------------------------------------
# API — GitLab stats
# ---------------------------------------------------------------------------

@router.get("/api/gitlab/stats")
async def get_gitlab_stats(request: Request, period: str = "week", start_date: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)

    if period == "today":
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        metrics = [
            {"label": "Commits",        "value": await _count(profile_id, "gitlab", "^commit", tw_s, tw_e)},
            {"label": "Merge Requests", "value": await _count(profile_id, "gitlab", "^mr_",    tw_s, tw_e)},
            {"label": "Issues",         "value": await _count(profile_id, "gitlab", "^issue",  tw_s, tw_e)},
        ]
    else:
        tw_s, tw_e = _week_bounds(0, tz_name)
        lw_s, lw_e = _week_bounds(1, tz_name)
        commits_n = await _count(profile_id, "gitlab", "^commit", tw_s, tw_e)
        commits_p = await _count(profile_id, "gitlab", "^commit", lw_s, lw_e)
        mrs_n     = await _count(profile_id, "gitlab", "^mr_",    tw_s, tw_e)
        mrs_p     = await _count(profile_id, "gitlab", "^mr_",    lw_s, lw_e)
        issues_n  = await _count(profile_id, "gitlab", "^issue",  tw_s, tw_e)
        issues_p  = await _count(profile_id, "gitlab", "^issue",  lw_s, lw_e)
        metrics = [
            {"label": "Commits",        "value": commits_n, "change": _pct(commits_n, commits_p)},
            {"label": "Merge Requests", "value": mrs_n,     "change": _pct(mrs_n,     mrs_p)},
            {"label": "Issues",         "value": issues_n,  "change": _pct(issues_n,  issues_p)},
        ]

    labels,  commits_daily = await _daily_counts(profile_id, "gitlab", r"^commit", days=7, tz_name=tz_name, start_date=start_date)
    _,       mr_daily      = await _daily_counts(profile_id, "gitlab", r"^mr_",    days=7, tz_name=tz_name, start_date=start_date)
    _,       issue_daily   = await _daily_counts(profile_id, "gitlab", r"^issue",  days=7, tz_name=tz_name, start_date=start_date)
    top_repos = await _top_items(profile_id, "gitlab")
    repos = {
        "commits":        await _workspace_breakdown(profile_id, "gitlab", r"^commit", tz_name=tz_name, start_date=start_date),
        "merge_requests": await _workspace_breakdown(profile_id, "gitlab", r"^mr_",    tz_name=tz_name, start_date=start_date),
        "issues":         await _workspace_breakdown(profile_id, "gitlab", r"^issue",  tz_name=tz_name, start_date=start_date),
    }

    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": {
                "commits":        commits_daily,
                "merge_requests": mr_daily,
                "issues":         issue_daily,
            },
            "repos": repos,
        },
        "top_items": top_repos,
        "top_label": "Top Projects",
    })


# ---------------------------------------------------------------------------
# API — Analytics / Summaries
# ---------------------------------------------------------------------------

@router.get("/api/summaries")
async def get_summaries(request: Request, limit: int = 10):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Summary)
                .where(Summary.profile_id == profile_id)
                .order_by(Summary.period_end.desc())
                .limit(limit)
            )
        ).scalars().all()

    result = []
    for s in rows:
        result.append({
            "id": str(s.id),
            "period_type": s.period_type,
            "period_start": s.period_start.isoformat() if s.period_start else None,
            "period_end": s.period_end.isoformat() if s.period_end else None,
            "content": s.content,
        })
    return JSONResponse({"summaries": result})


@router.get("/api/analytics/trend")
async def get_analytics_trend(request: Request, days: int = 28, group_by: str = "day", start_date: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    if start_date:
        sd = _dt.strptime(start_date, "%Y-%m-%d").date()
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

    # Build day labels in user's local time
    labels = []
    if start_date:
        sd = _dt.strptime(start_date, "%Y-%m-%d").date()
        for i in range(days):
            labels.append((sd + timedelta(days=i)).strftime("%Y-%m-%d"))
    else:
        for i in range(days - 1, -1, -1):
            d = now - timedelta(days=i)
            labels.append(d.strftime("%Y-%m-%d"))

    # Pivot: source -> {day -> count}
    pivot = {s: {day: 0 for day in labels} for s in sources}
    for r in results:
        src = r["_id"]["source"]
        day = r["_id"]["day"]
        if src in pivot and day in pivot[src]:
            pivot[src][day] = r["count"]

    if group_by == "week":
        week_data: dict = {}
        for day_str in labels:
            d = _dt.strptime(day_str, "%Y-%m-%d").date()
            monday = d - timedelta(days=d.weekday())
            monday_str = monday.strftime("%Y-%m-%d")
            if monday_str not in week_data:
                week_data[monday_str] = {s: 0 for s in sources}
            for src in sources:
                week_data[monday_str][src] += pivot[src].get(day_str, 0)
        sorted_weeks = sorted(week_data.keys())
        return JSONResponse({
            "labels":     [_dt.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in sorted_weeks],
            "raw_labels": sorted_weeks,
            "sources":    {s: [week_data[w][s] for w in sorted_weeks] for s in sources},
            "group_by":   "week",
        })

    # Event-type breakdown for richer tooltips
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

    display_labels = [_dt.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in labels]
    return JSONResponse({
        "labels":      display_labels,
        "raw_labels":  labels,
        "sources":     {s: list(pivot[s].values()) for s in sources},
        "event_types": event_types,
        "group_by":    "day",
    })


@router.post("/api/summaries/generate")
async def generate_summary(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    body = await request.json()
    period_type = body.get("period_type", "daily")
    specific_date = body.get("date")  # optional YYYY-MM-DD for past-day generation
    if period_type not in ("daily", "weekly"):
        return JSONResponse({"error": "invalid period_type"}, status_code=400)

    from app.ai.summarizer import _summarise_profile
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
    if not profile:
        return JSONResponse({"error": "profile_not_found"}, status_code=404)

    try:
        await _summarise_profile(profile, profile_id, period_type, full_day=True, specific_date=specific_date)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("On-demand summary failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/debug/today-events")
async def debug_today_events(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)

    # All events for this profile, no date filter
    all_cursor = activity_events().find(
        {"profile_id": profile_id}, {"raw_payload": 0}
    ).sort("occurred_at", -1)
    all_events = await all_cursor.to_list(length=20)

    # Also check if ANY events exist in the collection at all
    total_in_collection = await activity_events().count_documents({})

    # Check distinct profile_ids stored in MongoDB
    distinct_pids = await activity_events().distinct("profile_id")

    breakdown = {}
    for e in all_events:
        key = f"{e.get('source')}::{e.get('event_type')}"
        breakdown[key] = breakdown.get(key, 0) + 1

    recent = []
    for e in all_events[:5]:
        ts = e.get("occurred_at")
        recent.append({
            "source": e.get("source"),
            "event_type": e.get("event_type"),
            "occurred_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "title": e.get("title", "")[:60],
        })

    return JSONResponse({
        "session_profile_id": profile_id,
        "profile_timezone": profile.timezone if profile else None,
        "total_events_in_collection": total_in_collection,
        "distinct_profile_ids_in_mongo": distinct_pids,
        "events_for_this_profile": len(all_events),
        "breakdown": breakdown,
        "recent_5": recent,
    })


@router.get("/api/week-stats")
async def get_week_stats(request: Request, start: str = None, end: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(tz_name)
    try:
        start_dt = _dt.strptime(start, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
        end_dt   = (_dt.strptime(end, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid date"}, status_code=400)

    gh_commits = await _count(profile_id, "github", r"^commit",       start_dt, end_dt)
    gh_prs     = await _count(profile_id, "github", r"^pr_",          start_dt, end_dt)
    gh_issues  = await _count(profile_id, "github", r"^issue",        start_dt, end_dt)

    jira_created  = await _count(profile_id, "jira", "issue_created", start_dt, end_dt)
    jira_updated  = await _count(profile_id, "jira", "issue_updated", start_dt, end_dt)
    jira_comments = await _count(profile_id, "jira", "comment",       start_dt, end_dt)

    teams_msgs = await _count(profile_id, "teams_subscription", None, start_dt, end_dt)

    gl_commits = await _count(profile_id, "gitlab", r"^commit", start_dt, end_dt)
    gl_mrs     = await _count(profile_id, "gitlab", r"^mr_",    start_dt, end_dt)
    gl_issues  = await _count(profile_id, "gitlab", r"^issue",  start_dt, end_dt)

    return JSONResponse({
        "github": {"commits": gh_commits, "pull_requests": gh_prs,     "issues": gh_issues},
        "jira":   {"created": jira_created, "updated": jira_updated, "comments": jira_comments},
        "teams":  {"messages": teams_msgs},
        "gitlab": {"commits": gl_commits,   "merge_requests": gl_mrs,   "issues": gl_issues},
    })


@router.get("/api/day-data")
async def get_day_data(request: Request, date: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(tz_name)
    if not date:
        date = datetime.now(tz).strftime("%Y-%m-%d")
    try:
        day_start = _dt.strptime(date, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return JSONResponse({"error": "invalid date"}, status_code=400)
    day_end = day_start + timedelta(days=1)

    events_cursor = (
        activity_events()
        .find(
            {"profile_id": profile_id, "occurred_at": {"$gte": day_start, "$lt": day_end}},
        )
        .sort("occurred_at", -1)
    )
    raw_events = await events_cursor.to_list(length=500)

    source_counts = {"github": 0, "jira": 0, "teams_subscription": 0, "gitlab": 0}
    result_events = []
    for e in raw_events:
        ts  = e.get("occurred_at")
        src = e.get("source", "")
        raw = e.get("raw_payload") or {}
        if src in source_counts:
            source_counts[src] += 1
        sha   = None
        files = []
        if src == "github":
            raw_sha = e.get("source_event_id") or raw.get("after") or ""
            if raw_sha:
                sha = raw_sha[:7]
            head  = raw.get("head_commit") or {}
            files = ((head.get("modified") or []) + (head.get("added") or []) + (head.get("removed") or []))[:6]
        elif src == "gitlab":
            commits = raw.get("commits") or []
            if commits:
                raw_sha = commits[-1].get("id") or ""
                sha = raw_sha[:7] if raw_sha else None
                files = ((commits[-1].get("modified") or []) + (commits[-1].get("added") or []) + (commits[-1].get("removed") or []))[:6]
        result_events.append({
            "source": src,
            "event_type": e.get("event_type", ""),
            "title": e.get("title", ""),
            "workspace": e.get("workspace", ""),
            "occurred_at": (
                (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).isoformat()
                if isinstance(ts, datetime)
                else str(ts)
            ),
            "sha":   sha,
            "files": files,
        })

    async with AsyncSessionLocal() as db:
        summary_row = (
            await db.execute(
                select(Summary)
                .where(
                    Summary.profile_id == profile_id,
                    Summary.period_type == "daily",
                    Summary.period_start >= day_start,
                    Summary.period_start < day_end,
                )
                .order_by(Summary.period_end.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    return JSONResponse({
        "events": result_events,
        "source_counts": source_counts,
        "summary": summary_row.content if summary_row else None,
    })


@router.get("/api/week-breakdown")
async def get_week_breakdown(request: Request, start: str = None, end: str = None):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await _get_profile_tz(profile_id)
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(tz_name)

    try:
        start_local = _dt.strptime(start, "%Y-%m-%d").replace(tzinfo=tz)
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid date"}, status_code=400)

    today_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        end_local = _dt.strptime(end, "%Y-%m-%d").replace(tzinfo=tz)
    except (ValueError, TypeError):
        end_local = today_local

    # Never show future days
    end_local = min(end_local, today_local)

    # Build day list from start → end (inclusive)
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

    # Normalise naive timestamps to UTC-aware
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
                sha   = None
                files = []
                if src == "github":
                    raw_sha = e.get("source_event_id") or raw.get("after") or ""
                    if raw_sha:
                        sha = raw_sha[:7]
                    head  = raw.get("head_commit") or {}
                    files = ((head.get("modified") or []) + (head.get("added") or []) + (head.get("removed") or []))[:6]
                elif src == "gitlab":
                    commits = raw.get("commits") or []
                    if commits:
                        raw_sha = commits[-1].get("id") or ""
                        sha = raw_sha[:7] if raw_sha else None
                        files = ((commits[-1].get("modified") or []) + (commits[-1].get("added") or []) + (commits[-1].get("removed") or []))[:6]
                items.append({
                    "event_type":  e.get("event_type", ""),
                    "title":       e.get("title", "") or e.get("event_type", ""),
                    "workspace":   e.get("workspace", ""),
                    "occurred_at": e["occurred_at"].isoformat(),
                    "sha":         sha,
                    "files":       files,
                })
            connectors[src] = {"count": len(src_events), "items": items}

        result_days.append({"date": day.strftime("%Y-%m-%d"), "connectors": connectors})

    return JSONResponse({"days": result_days})


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get("session")
    profile_id = None
    if token:
        redis = get_redis()
        profile_id = await redis.get(f"session:{token}")
    if not profile_id:
        await websocket.close(code=4001)
        return
    pid = str(profile_id)
    await ws_manager.connect(websocket, pid)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, pid)


@router.delete("/api/summaries/{summary_id}")
async def delete_summary(request: Request, summary_id: str):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    import uuid as _uuid
    async with AsyncSessionLocal() as db:
        row = await db.get(Summary, _uuid.UUID(summary_id))
        if not row or str(row.profile_id) != str(profile_id):
            return JSONResponse({"error": "not_found"}, status_code=404)
        await db.delete(row)
        await db.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _fmt_day(dt) -> str:
    from datetime import datetime as _dt
    if isinstance(dt, _dt):
        return f"{dt.strftime('%A, %b')} {dt.day}"
    return "Unknown"


async def _fetch_day_events(profile_id: str, date_str: str):
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(await _get_profile_tz(profile_id))
    try:
        day_start = _dt.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return [], date_str
    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": day_start, "$lt": day_start + timedelta(days=1)}}
    ).sort("occurred_at", 1).to_list(length=500)
    d = _dt.strptime(date_str, "%Y-%m-%d")
    label = f"{d.strftime('%A, %B')} {d.day} {d.strftime('%Y')}"
    return events, label


async def _fetch_week_events(profile_id: str, week_start: str):
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(await _get_profile_tz(profile_id))
    try:
        ws = _dt.strptime(week_start, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return [], week_start
    we = ws + timedelta(days=7)
    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": ws, "$lt": we}}
    ).sort("occurred_at", 1).to_list(length=1000)
    ws_end = ws + timedelta(days=6)
    label = f"{ws.strftime('%b')} {ws.day} - {ws_end.strftime('%b')} {ws_end.day}, {ws_end.strftime('%Y')}"
    return events, label


async def _fetch_week_stats(profile_id: str, week_start: str) -> dict:
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(await _get_profile_tz(profile_id))
    try:
        ws = _dt.strptime(week_start, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return {}
    we = ws + timedelta(days=7)
    gh_commits = await _count(profile_id, "github",              r"^commit",      ws, we)
    gh_prs     = await _count(profile_id, "github",              r"^pr_",         ws, we)
    gh_issues  = await _count(profile_id, "github",              r"^issue",       ws, we)
    jira_c     = await _count(profile_id, "jira",                "issue_created", ws, we)
    jira_u     = await _count(profile_id, "jira",                "issue_updated", ws, we)
    jira_cmt   = await _count(profile_id, "jira",                "comment",       ws, we)
    teams_msg  = await _count(profile_id, "teams_subscription",  None,            ws, we)
    gl_commits = await _count(profile_id, "gitlab",              r"^commit",      ws, we)
    gl_mrs     = await _count(profile_id, "gitlab",              r"^mr_",         ws, we)
    gl_issues  = await _count(profile_id, "gitlab",              r"^issue",       ws, we)
    return {
        "github": {"commits": gh_commits, "pull_requests": gh_prs,  "issues": gh_issues},
        "jira":   {"created": jira_c,     "updated": jira_u,        "comments": jira_cmt},
        "teams":  {"messages": teams_msg},
        "gitlab": {"commits": gl_commits, "merge_requests": gl_mrs, "issues": gl_issues},
    }


async def _get_summary(profile_id: str, period_type: str, date_str: str) -> str:
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    tz = ZoneInfo(await _get_profile_tz(profile_id))
    try:
        ref = _dt.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return ""
    window = timedelta(days=7 if period_type == "weekly" else 1)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(Summary)
            .where(Summary.profile_id == profile_id, Summary.period_type == period_type,
                   Summary.period_start >= ref, Summary.period_start < ref + window)
            .order_by(Summary.period_end.desc()).limit(1)
        )).scalar_one_or_none()
    return row.content if row else ""


@router.get("/api/export/daily-pdf")
async def export_daily_pdf(request: Request, date: str = ""):
    try:
        profile_id = await get_profile_from_session(request)
        if not profile_id:
            return JSONResponse({"error": "not_authenticated"}, status_code=401)
        if isinstance(profile_id, bytes):
            profile_id = profile_id.decode()
        if not date:
            from zoneinfo import ZoneInfo
            from datetime import datetime as _dt
            date = _dt.now(ZoneInfo(await _get_profile_tz(profile_id))).strftime("%Y-%m-%d")
        events, label = await _fetch_day_events(profile_id, date)
        summary_text  = await _get_summary(profile_id, "daily", date)
        from app.services.export_pdf import generate_daily_pdf
        pdf_bytes = generate_daily_pdf(label, summary_text, events)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="daily-{date}.pdf"'})
    except Exception as _exc:
        logger.exception("daily-pdf failed: %s", _exc)
        return JSONResponse({"error": str(_exc), "type": type(_exc).__name__}, status_code=500)


@router.get("/api/export/weekly-pdf")
async def export_weekly_pdf(request: Request, week_start: str = ""):
    try:
        profile_id = await get_profile_from_session(request)
        if not profile_id:
            return JSONResponse({"error": "not_authenticated"}, status_code=401)
        if isinstance(profile_id, bytes):
            profile_id = profile_id.decode()
        if not week_start:
            from zoneinfo import ZoneInfo
            from datetime import datetime as _dt
            now = _dt.now(ZoneInfo(await _get_profile_tz(profile_id)))
            week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        events, label = await _fetch_week_events(profile_id, week_start)
        day_map: dict = {}
        for e in events:
            day_map.setdefault(_fmt_day(e.get("occurred_at")), []).append(e)
        counts: dict = {}
        for e in events:
            src = e.get("source", "other")
            counts[src] = counts.get(src, 0) + 1
        summary_text = await _get_summary(profile_id, "weekly", week_start)
        week_stats   = await _fetch_week_stats(profile_id, week_start)
        from app.services.export_pdf import generate_weekly_pdf
        pdf_bytes = generate_weekly_pdf(label, summary_text, list(day_map.items()), counts, week_stats)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="weekly-{week_start}.pdf"'})
    except Exception as _exc:
        logger.exception("weekly-pdf failed: %s", _exc)
        return JSONResponse({"error": str(_exc), "type": type(_exc).__name__}, status_code=500)
