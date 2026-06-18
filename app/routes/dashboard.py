import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.models import Integration, LinkedIdentity, Profile, Summary
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal

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


async def _daily_counts(profile_id, source=None, event_type_regex=None, days=7, tz_name: str = "UTC"):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name or "UTC")
    now = datetime.now(tz)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    q = {"profile_id": profile_id, "occurred_at": {"$gte": start}}
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
    for i in range(days - 1, -1, -1):
        day = now - timedelta(days=i)
        labels.append(day.strftime("%a"))
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
async def get_recent_events(request: Request, limit: int = 20):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    events = (
        await activity_events()
        .find({"profile_id": profile_id}, {"raw_payload": 0})
        .sort("occurred_at", -1)
        .to_list(length=limit)
    )
    result = []
    for e in events:
        ts = e.get("occurred_at")
        result.append({
            "source": e.get("source", ""),
            "event_type": e.get("event_type", ""),
            "title": e.get("title", ""),
            "workspace": e.get("workspace", ""),
            "occurred_at": (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).isoformat() if isinstance(ts, datetime) else str(ts),
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

    labels, counts = await _daily_counts(profile_id, days=7, tz_name=tz_name)

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
async def get_github_stats(request: Request, period: str = "week"):
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

    labels, counts = await _daily_counts(profile_id, "github", "^commit", days=7, tz_name=tz_name)
    top_repos = await _top_items(profile_id, "github")

    return JSONResponse({
        "metrics": metrics,
        "chart": {"labels": labels, "data": counts, "label": "Commits"},
        "top_items": top_repos,
        "top_label": "Top Repositories",
    })


# ---------------------------------------------------------------------------
# API — Jira stats
# ---------------------------------------------------------------------------

@router.get("/api/jira/stats")
async def get_jira_stats(request: Request, period: str = "week"):
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

    labels, counts = await _daily_counts(profile_id, "jira", days=7, tz_name=tz_name)
    top_projects = await _top_items(profile_id, "jira")

    return JSONResponse({
        "metrics": metrics,
        "chart": {"labels": labels, "data": counts, "label": "Issues"},
        "top_items": top_projects,
        "top_label": "Top Projects",
    })


# ---------------------------------------------------------------------------
# API — Teams stats
# ---------------------------------------------------------------------------

@router.get("/api/teams/stats")
async def get_teams_stats(request: Request, period: str = "week"):
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

    labels, counts = await _daily_counts(profile_id, "teams_subscription", days=7, tz_name=tz_name)

    return JSONResponse({
        "metrics": metrics,
        "chart": {"labels": labels, "data": counts, "label": "Messages"},
        "top_items": [],
        "top_label": "Top Channels",
    })


# ---------------------------------------------------------------------------
# API — GitLab stats
# ---------------------------------------------------------------------------

@router.get("/api/gitlab/stats")
async def get_gitlab_stats(request: Request, period: str = "week"):
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

    labels, counts = await _daily_counts(profile_id, "gitlab", "^commit", days=7, tz_name=tz_name)
    top_repos = await _top_items(profile_id, "gitlab")

    return JSONResponse({
        "metrics": metrics,
        "chart": {"labels": labels, "data": counts, "label": "Commits"},
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

    display_labels = [_dt.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in labels]
    return JSONResponse({
        "labels":     display_labels,
        "raw_labels": labels,
        "sources":    {s: list(pivot[s].values()) for s in sources},
        "group_by":   "day",
    })


@router.post("/api/summaries/generate")
async def generate_summary(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    body = await request.json()
    period_type = body.get("period_type", "daily")
    if period_type not in ("daily", "weekly"):
        return JSONResponse({"error": "invalid period_type"}, status_code=400)

    from app.ai.summarizer import _summarise_profile
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
    if not profile:
        return JSONResponse({"error": "profile_not_found"}, status_code=404)

    try:
        await _summarise_profile(profile, profile_id, period_type, full_day=False)
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
            {"raw_payload": 0},
        )
        .sort("occurred_at", -1)
    )
    raw_events = await events_cursor.to_list(length=500)

    source_counts = {"github": 0, "jira": 0, "teams_subscription": 0, "gitlab": 0}
    result_events = []
    for e in raw_events:
        ts = e.get("occurred_at")
        src = e.get("source", "")
        if src in source_counts:
            source_counts[src] += 1
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
        {"raw_payload": 0},
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
            connectors[src] = {
                "count": len(src_events),
                "items": [
                    {
                        "event_type": e.get("event_type", ""),
                        "title":      e.get("title", "") or e.get("event_type", ""),
                        "workspace":  e.get("workspace", ""),
                        "occurred_at": e["occurred_at"].isoformat(),
                    }
                    for e in src_events[:15]
                ],
            }

        result_days.append({"date": day.strftime("%Y-%m-%d"), "connectors": connectors})

    return JSONResponse({"days": result_days})


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
