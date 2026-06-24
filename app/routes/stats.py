import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import get_profile_from_session
from app.services.activity_query import (
    count, daily_counts, get_profile_tz, pct, top_items, week_bounds, workspace_breakdown,
)
from app.storage.postgres import get_db

router = APIRouter()


@router.get("/api/github/stats")
async def get_github_stats(request: Request, period: str = "week", start_date: str = None, db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await get_profile_tz(profile_id, db)

    if period == "today":
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        commits, prs, reviews, issues = await asyncio.gather(
            count(profile_id, "github", "^commit",      tw_s, tw_e),
            count(profile_id, "github", "^pr_",         tw_s, tw_e),
            count(profile_id, "github", "^pr_reviewed", tw_s, tw_e),
            count(profile_id, "github", "^issue",       tw_s, tw_e),
        )
        metrics = [
            {"label": "Pull Requests", "value": prs},
            {"label": "Commits",       "value": commits},
            {"label": "Reviews",       "value": reviews},
            {"label": "Issues",        "value": issues},
        ]
    else:
        tw_s, tw_e = week_bounds(0, tz_name)
        lw_s, lw_e = week_bounds(1, tz_name)
        (commits, prs, reviews, issues,
         commits_p, prs_p, reviews_p, issues_p) = await asyncio.gather(
            count(profile_id, "github", "^commit",      tw_s, tw_e),
            count(profile_id, "github", "^pr_",         tw_s, tw_e),
            count(profile_id, "github", "^pr_reviewed", tw_s, tw_e),
            count(profile_id, "github", "^issue",       tw_s, tw_e),
            count(profile_id, "github", "^commit",      lw_s, lw_e),
            count(profile_id, "github", "^pr_",         lw_s, lw_e),
            count(profile_id, "github", "^pr_reviewed", lw_s, lw_e),
            count(profile_id, "github", "^issue",       lw_s, lw_e),
        )
        metrics = [
            {"label": "Pull Requests", "value": prs,     "change": pct(prs,     prs_p)},
            {"label": "Commits",       "value": commits, "change": pct(commits, commits_p)},
            {"label": "Reviews",       "value": reviews, "change": pct(reviews, reviews_p)},
            {"label": "Issues",        "value": issues,  "change": pct(issues,  issues_p)},
        ]

    (
        (labels, commits_daily),
        (_, pr_daily),
        (_, issue_daily),
        (_, review_daily),
        top_repos,
        repos_commits,
        repos_prs,
        repos_issues,
        repos_reviews,
    ) = await asyncio.gather(
        daily_counts(profile_id, "github", r"^commit",      days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "github", r"^pr_",         days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "github", r"^issue",       days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "github", r"^pr_reviewed", days=7, tz_name=tz_name, start_date=start_date),
        top_items(profile_id, "github"),
        workspace_breakdown(profile_id, "github", r"^commit",      tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "github", r"^pr_",         tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "github", r"^issue",       tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "github", r"^pr_reviewed", tz_name=tz_name, start_date=start_date),
    )
    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": {
                "commits": commits_daily, "pull_requests": pr_daily,
                "issues":  issue_daily,   "reviews":       review_daily,
            },
            "repos": {
                "commits": repos_commits, "pull_requests": repos_prs,
                "issues":  repos_issues,  "reviews":       repos_reviews,
            },
        },
        "top_items": top_repos,
        "top_label": "Top Repositories",
    })


@router.get("/api/jira/stats")
async def get_jira_stats(request: Request, period: str = "week", start_date: str = None, db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await get_profile_tz(profile_id, db)

    if period == "today":
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        created_n, updated_n, comments_n, total_n = await asyncio.gather(
            count(profile_id, "jira", "issue_created", tw_s, tw_e),
            count(profile_id, "jira", "issue_updated", tw_s, tw_e),
            count(profile_id, "jira", "comment",       tw_s, tw_e),
            count(profile_id, "jira", None,            tw_s, tw_e),
        )
        metrics = [
            {"label": "Created",  "value": created_n},
            {"label": "Updated",  "value": updated_n},
            {"label": "Comments", "value": comments_n},
            {"label": "Total",    "value": total_n},
        ]
    else:
        tw_s, tw_e = week_bounds(0, tz_name)
        lw_s, lw_e = week_bounds(1, tz_name)
        (created_n, updated_n, comments_n, total_n,
         created_p, updated_p, comments_p, total_p) = await asyncio.gather(
            count(profile_id, "jira", "issue_created", tw_s, tw_e),
            count(profile_id, "jira", "issue_updated", tw_s, tw_e),
            count(profile_id, "jira", "comment",       tw_s, tw_e),
            count(profile_id, "jira", None,            tw_s, tw_e),
            count(profile_id, "jira", "issue_created", lw_s, lw_e),
            count(profile_id, "jira", "issue_updated", lw_s, lw_e),
            count(profile_id, "jira", "comment",       lw_s, lw_e),
            count(profile_id, "jira", None,            lw_s, lw_e),
        )
        metrics = [
            {"label": "Created",  "value": created_n,  "change": pct(created_n,  created_p)},
            {"label": "Updated",  "value": updated_n,  "change": pct(updated_n,  updated_p)},
            {"label": "Comments", "value": comments_n, "change": pct(comments_n, comments_p)},
            {"label": "Total",    "value": total_n,    "change": pct(total_n,    total_p)},
        ]

    (
        (labels, created_daily),
        (_, updated_daily),
        (_, comment_daily),
        top_projects,
        repos_created,
        repos_updated,
        repos_comments,
    ) = await asyncio.gather(
        daily_counts(profile_id, "jira", "issue_created", days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "jira", "issue_updated", days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "jira", "comment",       days=7, tz_name=tz_name, start_date=start_date),
        top_items(profile_id, "jira"),
        workspace_breakdown(profile_id, "jira", "issue_created", tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "jira", "issue_updated", tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "jira", "comment",       tz_name=tz_name, start_date=start_date),
    )
    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": {
                "created": created_daily, "updated": updated_daily, "comments": comment_daily,
            },
            "repos": {
                "created": repos_created, "updated": repos_updated, "comments": repos_comments,
            },
        },
        "top_items": top_projects,
        "top_label": "Top Projects",
    })


@router.get("/api/teams/stats")
async def get_teams_stats(request: Request, period: str = "week", start_date: str = None, db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await get_profile_tz(profile_id, db)

    if period == "today":
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        msgs = await count(profile_id, "teams_subscription", None, tw_s, tw_e)
        metrics = [{"label": "Messages", "value": msgs}]
    else:
        tw_s, tw_e = week_bounds(0, tz_name)
        lw_s, lw_e = week_bounds(1, tz_name)
        msgs_now  = await count(profile_id, "teams_subscription", None, tw_s, tw_e)
        msgs_prev = await count(profile_id, "teams_subscription", None, lw_s, lw_e)
        metrics = [{"label": "Messages", "value": msgs_now, "change": pct(msgs_now, msgs_prev)}]

    labels, messages_daily = await daily_counts(profile_id, "teams_subscription", days=7, tz_name=tz_name, start_date=start_date)
    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": {"messages": messages_daily},
            "repos": {"messages": await workspace_breakdown(profile_id, "teams_subscription", tz_name=tz_name, start_date=start_date)},
        },
        "top_items": [],
        "top_label": "Top Channels",
    })


@router.get("/api/gitlab/stats")
async def get_gitlab_stats(request: Request, period: str = "week", start_date: str = None, db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    tz_name = await get_profile_tz(profile_id, db)

    if period == "today":
        now_local = datetime.now(ZoneInfo(tz_name))
        tw_s = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        tw_e = datetime.now(timezone.utc)
        commits_n, mrs_n, issues_n, notes_n, pipelines_n, tags_n = await asyncio.gather(
            count(profile_id, "gitlab", "^commit",        tw_s, tw_e),
            count(profile_id, "gitlab", "^merge_request", tw_s, tw_e),
            count(profile_id, "gitlab", "^issue",         tw_s, tw_e),
            count(profile_id, "gitlab", "^note",          tw_s, tw_e),
            count(profile_id, "gitlab", "^pipeline",      tw_s, tw_e),
            count(profile_id, "gitlab", "^tag_push",      tw_s, tw_e),
        )
        metrics = [
            {"label": "Commits",        "value": commits_n},
            {"label": "Merge Requests", "value": mrs_n},
            {"label": "Issues",         "value": issues_n},
            {"label": "Comments",       "value": notes_n},
            {"label": "Pipelines",      "value": pipelines_n},
            {"label": "Tags",           "value": tags_n},
        ]
    else:
        tw_s, tw_e = week_bounds(0, tz_name)
        lw_s, lw_e = week_bounds(1, tz_name)
        (commits_n, mrs_n, issues_n, notes_n, pipelines_n, tags_n,
         commits_p, mrs_p, issues_p, notes_p, pipelines_p, tags_p) = await asyncio.gather(
            count(profile_id, "gitlab", "^commit",        tw_s, tw_e),
            count(profile_id, "gitlab", "^merge_request", tw_s, tw_e),
            count(profile_id, "gitlab", "^issue",         tw_s, tw_e),
            count(profile_id, "gitlab", "^note",          tw_s, tw_e),
            count(profile_id, "gitlab", "^pipeline",      tw_s, tw_e),
            count(profile_id, "gitlab", "^tag_push",      tw_s, tw_e),
            count(profile_id, "gitlab", "^commit",        lw_s, lw_e),
            count(profile_id, "gitlab", "^merge_request", lw_s, lw_e),
            count(profile_id, "gitlab", "^issue",         lw_s, lw_e),
            count(profile_id, "gitlab", "^note",          lw_s, lw_e),
            count(profile_id, "gitlab", "^pipeline",      lw_s, lw_e),
            count(profile_id, "gitlab", "^tag_push",      lw_s, lw_e),
        )
        metrics = [
            {"label": "Commits",        "value": commits_n,   "change": pct(commits_n,   commits_p)},
            {"label": "Merge Requests", "value": mrs_n,       "change": pct(mrs_n,       mrs_p)},
            {"label": "Issues",         "value": issues_n,    "change": pct(issues_n,    issues_p)},
            {"label": "Comments",       "value": notes_n,     "change": pct(notes_n,     notes_p)},
            {"label": "Pipelines",      "value": pipelines_n, "change": pct(pipelines_n, pipelines_p)},
            {"label": "Tags",           "value": tags_n,      "change": pct(tags_n,      tags_p)},
        ]

    (
        (labels, commits_daily),
        (_, mr_daily),
        (_, issue_daily),
        (_, notes_daily),
        (_, pipeline_daily),
        top_repos,
        repos_commits,
        repos_mrs,
        repos_issues,
        repos_notes,
        repos_pipelines,
    ) = await asyncio.gather(
        daily_counts(profile_id, "gitlab", r"^commit",        days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "gitlab", r"^merge_request", days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "gitlab", r"^issue",         days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "gitlab", r"^note",          days=7, tz_name=tz_name, start_date=start_date),
        daily_counts(profile_id, "gitlab", r"^pipeline",      days=7, tz_name=tz_name, start_date=start_date),
        top_items(profile_id, "gitlab"),
        workspace_breakdown(profile_id, "gitlab", r"^commit",        tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "gitlab", r"^merge_request", tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "gitlab", r"^issue",         tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "gitlab", r"^note",          tz_name=tz_name, start_date=start_date),
        workspace_breakdown(profile_id, "gitlab", r"^pipeline",      tz_name=tz_name, start_date=start_date),
    )
    return JSONResponse({
        "metrics": metrics,
        "chart": {
            "labels": labels,
            "datasets": {
                "commits": commits_daily, "merge_requests": mr_daily,
                "issues":  issue_daily,   "notes":          notes_daily,
                "pipelines": pipeline_daily,
            },
            "repos": {
                "commits": repos_commits, "merge_requests": repos_mrs,
                "issues":  repos_issues,  "notes":          repos_notes,
                "pipelines": repos_pipelines,
            },
        },
        "top_items": top_repos,
        "top_label": "Top Projects",
    })
