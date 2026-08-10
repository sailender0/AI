import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import require_profile
from app.services.activity_query import (
    count, daily_counts, get_profile_tz, pct, top_items, week_bounds, workspace_breakdown,
)
from app.services.jira_board import fetch_assigned
from app.services.timezone import day_bounds, resolve, today_str
from app.storage.postgres import get_db

router = APIRouter()


@router.get("/api/jira/assigned")
async def get_jira_assigned(profile_id: str = Depends(require_profile)):
    data = await fetch_assigned(profile_id)
    if data is None:
        return JSONResponse({"error": "jira_unavailable"}, status_code=502)
    return JSONResponse(data)


_STATS = {
    "github": {
        "metrics": [("Pull Requests", r"^pr_(?!review$)"), ("Commits", r"^commit"),
                    ("Reviews", r"^pr_review$"), ("Issues", r"^issue")],
        "series":  [("commits", r"^commit"), ("pull_requests", r"^pr_(?!review$)"),
                    ("issues", r"^issue"), ("reviews", r"^pr_review$")],
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
                        period: str, start_date: str | None) -> dict:
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
    return out


@router.get("/api/github/stats")
async def get_github_stats(period: str = "week", start_date: str = None,
                           profile_id: str = Depends(require_profile),
                           db: AsyncSession = Depends(get_db)):
    return JSONResponse(await _source_stats(profile_id, db, "github", period, start_date))


@router.get("/api/gitlab/stats")
async def get_gitlab_stats(period: str = "week", start_date: str = None,
                           profile_id: str = Depends(require_profile),
                           db: AsyncSession = Depends(get_db)):
    return JSONResponse(await _source_stats(profile_id, db, "gitlab", period, start_date))


@router.get("/api/jira/stats")
async def get_jira_stats(start_date: str = None,
                         profile_id: str = Depends(require_profile),
                         db: AsyncSession = Depends(get_db)):
    return JSONResponse(await _source_stats(profile_id, db, "jira", "week", start_date))


@router.get("/api/teams/stats")
async def get_teams_stats(period: str = "week", start_date: str = None,
                          profile_id: str = Depends(require_profile),
                          db: AsyncSession = Depends(get_db)):
    return JSONResponse(await _source_stats(profile_id, db, "teams_subscription", period, start_date))
