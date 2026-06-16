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

def _week_bounds(weeks_ago: int = 0):
    now = datetime.now(timezone.utc)
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    start = monday - timedelta(weeks=weeks_ago)
    end = start + timedelta(weeks=1)
    return start, end


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


async def _daily_counts(profile_id, source=None, event_type_regex=None, days=7):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    q = {"profile_id": profile_id, "occurred_at": {"$gte": start}}
    if source:
        q["source"] = source
    if event_type_regex:
        q["event_type"] = {"$regex": event_type_regex}
    pipeline = [
        {"$match": q},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at"}}, "count": {"$sum": 1}}},
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
async def get_stats(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tw_s, tw_e = _week_bounds(0)
    lw_s, lw_e = _week_bounds(1)

    commits_now  = await _count(profile_id, "github", "^commit", tw_s, tw_e)
    commits_prev = await _count(profile_id, "github", "^commit", lw_s, lw_e)
    prs_now      = await _count(profile_id, "github", "^pr_",    tw_s, tw_e)
    prs_prev     = await _count(profile_id, "github", "^pr_",    lw_s, lw_e)
    issues_now   = await _count(profile_id, "jira",   None,      tw_s, tw_e)
    issues_prev  = await _count(profile_id, "jira",   None,      lw_s, lw_e)
    meetings_now  = await _count(profile_id, "teams_subscription", None, tw_s, tw_e)
    meetings_prev = await _count(profile_id, "teams_subscription", None, lw_s, lw_e)

    total_now  = commits_now + prs_now * 2 + issues_now * 1.5 + meetings_now
    total_prev = commits_prev + prs_prev * 2 + issues_prev * 1.5 + meetings_prev
    score = min(100, round((total_now / max(1, total_prev)) * 65)) if total_prev else min(100, int(total_now * 3))

    labels, counts = await _daily_counts(profile_id, days=7)

    return JSONResponse({
        "metrics": [
            {"label": "Commits",      "value": commits_now,  "change": _pct(commits_now,  commits_prev),  "icon": "commit"},
            {"label": "Pull Requests","value": prs_now,      "change": _pct(prs_now,      prs_prev),      "icon": "pr"},
            {"label": "Jira Issues",  "value": issues_now,   "change": _pct(issues_now,   issues_prev),   "icon": "jira"},
            {"label": "Meetings",     "value": meetings_now, "change": _pct(meetings_now, meetings_prev), "icon": "meeting"},
        ],
        "ai_score": score,
        "chart": {"labels": labels, "data": counts},
    })


# ---------------------------------------------------------------------------
# API — GitHub stats
# ---------------------------------------------------------------------------

@router.get("/api/github/stats")
async def get_github_stats(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tw_s, tw_e = _week_bounds(0)
    lw_s, lw_e = _week_bounds(1)

    commits_now   = await _count(profile_id, "github", "^commit",      tw_s, tw_e)
    commits_prev  = await _count(profile_id, "github", "^commit",      lw_s, lw_e)
    prs_now       = await _count(profile_id, "github", "^pr_",         tw_s, tw_e)
    prs_prev      = await _count(profile_id, "github", "^pr_",         lw_s, lw_e)
    reviews_now   = await _count(profile_id, "github", "^pr_reviewed", tw_s, tw_e)
    reviews_prev  = await _count(profile_id, "github", "^pr_reviewed", lw_s, lw_e)
    issues_now    = await _count(profile_id, "github", "^issue",       tw_s, tw_e)
    issues_prev   = await _count(profile_id, "github", "^issue",       lw_s, lw_e)

    labels, counts = await _daily_counts(profile_id, "github", "^commit", days=7)
    top_repos = await _top_items(profile_id, "github")

    return JSONResponse({
        "metrics": [
            {"label": "Pull Requests", "value": prs_now,     "change": _pct(prs_now,     prs_prev)},
            {"label": "Commits",       "value": commits_now, "change": _pct(commits_now, commits_prev)},
            {"label": "Reviews",       "value": reviews_now, "change": _pct(reviews_now, reviews_prev)},
            {"label": "Issues",        "value": issues_now,  "change": _pct(issues_now,  issues_prev)},
        ],
        "chart": {"labels": labels, "data": counts, "label": "Commits"},
        "top_items": top_repos,
        "top_label": "Top Repositories",
    })


# ---------------------------------------------------------------------------
# API — Jira stats
# ---------------------------------------------------------------------------

@router.get("/api/jira/stats")
async def get_jira_stats(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tw_s, tw_e = _week_bounds(0)
    lw_s, lw_e = _week_bounds(1)

    created_now   = await _count(profile_id, "jira", "issue_created", tw_s, tw_e)
    created_prev  = await _count(profile_id, "jira", "issue_created", lw_s, lw_e)
    updated_now   = await _count(profile_id, "jira", "issue_updated", tw_s, tw_e)
    updated_prev  = await _count(profile_id, "jira", "issue_updated", lw_s, lw_e)
    comments_now  = await _count(profile_id, "jira", "comment",       tw_s, tw_e)
    comments_prev = await _count(profile_id, "jira", "comment",       lw_s, lw_e)
    total_now     = await _count(profile_id, "jira", None,            tw_s, tw_e)
    total_prev    = await _count(profile_id, "jira", None,            lw_s, lw_e)

    labels, counts = await _daily_counts(profile_id, "jira", days=7)
    top_projects = await _top_items(profile_id, "jira")

    return JSONResponse({
        "metrics": [
            {"label": "Created",    "value": created_now,  "change": _pct(created_now,  created_prev)},
            {"label": "Updated",    "value": updated_now,  "change": _pct(updated_now,  updated_prev)},
            {"label": "Comments",   "value": comments_now, "change": _pct(comments_now, comments_prev)},
            {"label": "Total",      "value": total_now,    "change": _pct(total_now,    total_prev)},
        ],
        "chart": {"labels": labels, "data": counts, "label": "Issues"},
        "top_items": top_projects,
        "top_label": "Top Projects",
    })


# ---------------------------------------------------------------------------
# API — Teams stats
# ---------------------------------------------------------------------------

@router.get("/api/teams/stats")
async def get_teams_stats(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tw_s, tw_e = _week_bounds(0)
    lw_s, lw_e = _week_bounds(1)

    msgs_now  = await _count(profile_id, "teams_subscription", None, tw_s, tw_e)
    msgs_prev = await _count(profile_id, "teams_subscription", None, lw_s, lw_e)

    labels, counts = await _daily_counts(profile_id, "teams_subscription", days=7)

    return JSONResponse({
        "metrics": [
            {"label": "Messages", "value": msgs_now, "change": _pct(msgs_now, msgs_prev)},
        ],
        "chart": {"labels": labels, "data": counts, "label": "Messages"},
        "top_items": [],
        "top_label": "Top Channels",
    })


# ---------------------------------------------------------------------------
# API — GitLab stats
# ---------------------------------------------------------------------------

@router.get("/api/gitlab/stats")
async def get_gitlab_stats(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tw_s, tw_e = _week_bounds(0)
    lw_s, lw_e = _week_bounds(1)

    commits_now  = await _count(profile_id, "gitlab", "^commit", tw_s, tw_e)
    commits_prev = await _count(profile_id, "gitlab", "^commit", lw_s, lw_e)
    mrs_now      = await _count(profile_id, "gitlab", "^mr_",    tw_s, tw_e)
    mrs_prev     = await _count(profile_id, "gitlab", "^mr_",    lw_s, lw_e)
    issues_now   = await _count(profile_id, "gitlab", "^issue",  tw_s, tw_e)
    issues_prev  = await _count(profile_id, "gitlab", "^issue",  lw_s, lw_e)

    labels, counts = await _daily_counts(profile_id, "gitlab", "^commit", days=7)
    top_repos = await _top_items(profile_id, "gitlab")

    return JSONResponse({
        "metrics": [
            {"label": "Commits",         "value": commits_now, "change": _pct(commits_now, commits_prev)},
            {"label": "Merge Requests",  "value": mrs_now,     "change": _pct(mrs_now,     mrs_prev)},
            {"label": "Issues",          "value": issues_now,  "change": _pct(issues_now,  issues_prev)},
        ],
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
async def get_analytics_trend(request: Request, days: int = 28):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    sources = ["github", "jira", "teams_subscription", "gitlab"]
    pipeline = [
        {"$match": {"profile_id": profile_id, "occurred_at": {"$gte": start}}},
        {"$group": {
            "_id": {
                "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$occurred_at"}},
                "source": "$source",
            },
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id.day": 1}},
    ]
    results = await activity_events().aggregate(pipeline).to_list(length=None)

    # Build day labels
    labels = []
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

    display_labels = [(datetime.strptime(d, "%Y-%m-%d")).strftime("%b %-d") if hasattr(datetime, "strptime") else d for d in labels]
    # safe cross-platform label formatting
    from datetime import datetime as _dt
    display_labels = [_dt.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in labels]

    return JSONResponse({
        "labels": display_labels,
        "sources": {s: list(pivot[s].values()) for s in sources},
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
        await _summarise_profile(profile, profile_id, period_type)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("On-demand summary failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


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
