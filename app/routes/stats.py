import asyncio

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oauth import get_valid_token, mark_integration_error
from app.auth.sso import require_profile
from app.services.activity_query import (
    count, daily_counts, get_profile_tz, pct, top_items, week_bounds, workspace_breakdown,
)
from app.services.timezone import day_bounds, resolve, today_str
from app.storage.postgres import get_db

router = APIRouter()


def _issue_row(issue: dict) -> dict:
    """Pure mapper: a /search/jql issue -> one 'assigned to me' panel row."""
    f = issue.get("fields") or {}
    # ponytail: 10016/10020 are the common story-points/sprint field ids; if a
    # site differs, discover the real ids via GET /rest/api/3/field.
    sprints = f.get("customfield_10020") or []
    active = next((s.get("name") for s in sprints
                   if isinstance(s, dict) and s.get("state") == "active"), None)
    last = sprints[-1].get("name") if sprints and isinstance(sprints[-1], dict) else None
    return {
        "key":             issue.get("key", ""),
        "summary":         f.get("summary") or "",
        "status":          (f.get("status") or {}).get("name") or "",
        "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key", ""),
        "priority":        (f.get("priority") or {}).get("name") or "",
        "issue_type":      (f.get("issuetype") or {}).get("name") or "",
        "due_date":        f.get("duedate"),
        "created":         f.get("created"),
        "story_points":    f.get("customfield_10016"),
        "sprint":          active or last,
    }


async def fetch_assigned(profile_id: str) -> dict | None:
    """Live 'assigned to me' state from Jira: {site_url, done_7d, issues}, or
    None when the connection is missing/broken. Shared by the API route and the
    AI chat context. Doubles as the connection health probe: a 401/403 from
    Atlassian flags the integration (amber dot + reconnect banner)."""
    token = await get_valid_token(profile_id, "jira")
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            "https://api.atlassian.com/oauth/token/accessible-resources", headers=headers)
        if res.status_code in (401, 403):
            await mark_integration_error(profile_id, "jira")
        if res.status_code != 200 or not res.json():
            return None
        site = res.json()[0]
        r = await client.get(
            f"https://api.atlassian.com/ex/jira/{site['id']}/rest/api/3/search/jql",
            headers=headers,
            params={
                "jql": "assignee = currentUser() AND statusCategory != Done"
                       " ORDER BY priority DESC, updated DESC",
                # ponytail: one page of 50; paginate with nextPageToken if
                # anyone actually has 50+ open issues.
                "maxResults": 50,
                "fields": "summary,status,priority,issuetype,duedate,created,"
                          "customfield_10016,customfield_10020",
            },
        )
        if r.status_code in (401, 403):
            await mark_integration_error(profile_id, "jira")
        if r.status_code != 200:
            return None

        # momentum KPI — open-issues JQL can't see finished work, so ask the
        # count endpoint; None (tile shows "—") when Atlassian errors
        done = await client.post(
            f"https://api.atlassian.com/ex/jira/{site['id']}/rest/api/3/search/approximate-count",
            headers=headers,
            json={"jql": "assignee = currentUser() AND statusCategory = Done AND resolved >= -7d"},
        )
        done_7d = done.json().get("count") if done.status_code == 200 else None

    return {
        "site_url": site.get("url", ""),
        "done_7d": done_7d,
        "issues": [_issue_row(it) for it in r.json().get("issues", [])],
    }


@router.get("/api/jira/assigned")
async def get_jira_assigned(profile_id: str = Depends(require_profile)):
    data = await fetch_assigned(profile_id)
    if data is None:
        return JSONResponse({"error": "jira_unavailable"}, status_code=502)
    return JSONResponse(data)


# ── Per-source stats ──────────────────────────────────────────────────────────
# The four /api/<source>/stats endpoints are one query shape with different
# labels/regexes; this table + _source_stats replaces the hand-rolled copies.
# "metrics": KPI tiles (None = source has no tile block — Jira's KPIs come live
# from /api/jira/assigned). "series": chart datasets + per-repo breakdowns.

_STATS = {
    "github": {
        "metrics": [("Pull Requests", r"^pr_"), ("Commits", r"^commit"),
                    ("Reviews", r"^pr_reviewed"), ("Issues", r"^issue")],
        "series":  [("commits", r"^commit"), ("pull_requests", r"^pr_"),
                    ("issues", r"^issue"), ("reviews", r"^pr_reviewed")],
        "top_label": "Top Repositories", "has_top": True,
    },
    "gitlab": {
        "metrics": [("Commits", r"^commit"), ("Merge Requests", r"^merge_request"),
                    ("Issues", r"^issue"), ("Comments", r"^note"),
                    ("Pipelines", r"^pipeline"), ("Tags", r"^tag_push")],
        "series":  [("commits", r"^commit"), ("merge_requests", r"^merge_request"),
                    ("issues", r"^issue"), ("notes", r"^note"), ("pipelines", r"^pipeline")],
        "top_label": "Top Projects", "has_top": True,
    },
    "jira": {
        "metrics": None,
        "series":  [("created", "issue_created"), ("updated", "issue_updated"),
                    ("comments", "comment")],
        "top_label": "Top Projects", "has_top": True,
    },
    "teams_subscription": {
        "metrics": [("Messages", None)],
        "series":  [("messages", None)],
        "top_label": "Top Channels", "has_top": False,
    },
}


async def _metric_tiles(profile_id: str, source: str, specs: list, period: str,
                        tz_name: str) -> list[dict]:
    if period == "today":
        tz = resolve(tz_name)
        tw_s, tw_e = day_bounds(today_str(tz), tz)
        vals = await asyncio.gather(
            *(count(profile_id, source, rx, tw_s, tw_e) for _, rx in specs))
        return [{"label": lbl, "value": v} for (lbl, _), v in zip(specs, vals)]

    tw_s, tw_e = week_bounds(0, tz_name)
    lw_s, lw_e = week_bounds(1, tz_name)
    vals = await asyncio.gather(
        *(count(profile_id, source, rx, tw_s, tw_e) for _, rx in specs),
        *(count(profile_id, source, rx, lw_s, lw_e) for _, rx in specs),
    )
    cur, prev = vals[:len(specs)], vals[len(specs):]
    return [{"label": lbl, "value": c, "change": pct(c, p)}
            for (lbl, _), c, p in zip(specs, cur, prev)]


async def _source_stats(profile_id: str, db: AsyncSession, source: str,
                        period: str, start_date: str | None) -> JSONResponse:
    tz_name = await get_profile_tz(profile_id, db)
    cfg     = _STATS[source]
    series  = cfg["series"]

    daily, repos, top = await asyncio.gather(
        asyncio.gather(*(daily_counts(profile_id, source, rx, days=7,
                                      tz_name=tz_name, start_date=start_date)
                         for _, rx in series)),
        asyncio.gather(*(workspace_breakdown(profile_id, source, rx,
                                             tz_name=tz_name, start_date=start_date)
                         for _, rx in series)),
        top_items(profile_id, source) if cfg["has_top"] else asyncio.sleep(0, result=[]),
    )
    out = {
        "chart": {
            "labels":   daily[0][0],
            "datasets": {key: counts for (key, _), (_, counts) in zip(series, daily)},
            "repos":    {key: ws for (key, _), ws in zip(series, repos)},
        },
        "top_items": top,
        "top_label": cfg["top_label"],
    }
    if cfg["metrics"] is not None:
        out["metrics"] = await _metric_tiles(profile_id, source, cfg["metrics"], period, tz_name)
    return JSONResponse(out)


@router.get("/api/github/stats")
async def get_github_stats(period: str = "week", start_date: str = None,
                           profile_id: str = Depends(require_profile),
                           db: AsyncSession = Depends(get_db)):
    return await _source_stats(profile_id, db, "github", period, start_date)


@router.get("/api/gitlab/stats")
async def get_gitlab_stats(period: str = "week", start_date: str = None,
                           profile_id: str = Depends(require_profile),
                           db: AsyncSession = Depends(get_db)):
    return await _source_stats(profile_id, db, "gitlab", period, start_date)


@router.get("/api/jira/stats")
async def get_jira_stats(start_date: str = None,
                         profile_id: str = Depends(require_profile),
                         db: AsyncSession = Depends(get_db)):
    return await _source_stats(profile_id, db, "jira", "week", start_date)


@router.get("/api/teams/stats")
async def get_teams_stats(period: str = "week", start_date: str = None,
                          profile_id: str = Depends(require_profile),
                          db: AsyncSession = Depends(get_db)):
    return await _source_stats(profile_id, db, "teams_subscription", period, start_date)
