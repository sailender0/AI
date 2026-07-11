import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import get_profile_from_session
from app.services.activity_query import get_profile_tz
from app.storage.models import Summary
from app.storage.mongodb import activity_events
from app.storage.postgres import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


def _fmt_day(dt) -> str:
    if isinstance(dt, datetime):
        return f"{dt.strftime('%A, %b')} {dt.day}"
    return "Unknown"


async def _fetch_day_events(profile_id: str, date_str: str, db: AsyncSession):
    tz = ZoneInfo(await get_profile_tz(profile_id, db))
    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return [], date_str
    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": day_start, "$lt": day_start + timedelta(days=1)}}
    ).sort("occurred_at", 1).to_list(length=500)
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return events, f"{d.strftime('%A, %B')} {d.day} {d.strftime('%Y')}"


async def _fetch_week_events(profile_id: str, week_start: str, db: AsyncSession):
    tz = ZoneInfo(await get_profile_tz(profile_id, db))
    try:
        ws = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return [], week_start
    we = ws + timedelta(days=7)
    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": ws, "$lt": we}}
    ).sort("occurred_at", 1).to_list(length=1000)
    ws_end = ws + timedelta(days=6)
    return events, f"{ws.strftime('%b')} {ws.day} - {ws_end.strftime('%b')} {ws_end.day}, {ws_end.strftime('%Y')}"


async def _fetch_week_stats(profile_id: str, week_start: str, db: AsyncSession) -> dict:
    from app.services.activity_query import count
    tz = ZoneInfo(await get_profile_tz(profile_id, db))
    try:
        ws = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return {}
    we = ws + timedelta(days=7)
    gh_commits = await count(profile_id, "github",             r"^commit",      ws, we)
    gh_prs     = await count(profile_id, "github",             r"^pr_",         ws, we)
    gh_issues  = await count(profile_id, "github",             r"^issue",       ws, we)
    jira_c     = await count(profile_id, "jira",               "issue_created", ws, we)
    jira_u     = await count(profile_id, "jira",               "issue_updated", ws, we)
    jira_cmt   = await count(profile_id, "jira",               "comment",       ws, we)
    teams_msg  = await count(profile_id, "teams_subscription",  None,           ws, we)
    gl_commits = await count(profile_id, "gitlab",             r"^commit",      ws, we)
    gl_mrs     = await count(profile_id, "gitlab",             r"^merge_request", ws, we)
    gl_issues  = await count(profile_id, "gitlab",             r"^issue",       ws, we)
    return {
        "github": {"commits": gh_commits, "pull_requests": gh_prs,  "issues": gh_issues},
        "jira":   {"created": jira_c,     "updated": jira_u,        "comments": jira_cmt},
        "teams":  {"messages": teams_msg},
        "gitlab": {"commits": gl_commits, "merge_requests": gl_mrs, "issues": gl_issues},
    }


async def _get_summary(profile_id: str, period_type: str, date_str: str, db: AsyncSession) -> str:
    tz = ZoneInfo(await get_profile_tz(profile_id, db))
    try:
        ref = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz).astimezone(timezone.utc)
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


@router.get("/api/export/daily-pdf")
async def export_daily_pdf(request: Request, date: str = "", db: AsyncSession = Depends(get_db)):
    try:
        profile_id = await get_profile_from_session(request)
        if not profile_id:
            return JSONResponse({"error": "not_authenticated"}, status_code=401)
        if not date:
            date = datetime.now(ZoneInfo(await get_profile_tz(profile_id, db))).strftime("%Y-%m-%d")
        events, label = await _fetch_day_events(profile_id, date, db)
        summary_text  = await _get_summary(profile_id, "daily", date, db)
        from app.services.export_pdf import generate_daily_pdf
        pdf_bytes = generate_daily_pdf(label, summary_text, events)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="daily-{date}.pdf"'})
    except Exception as _exc:
        logger.exception("daily-pdf failed: %s", _exc)
        return JSONResponse({"error": "export_failed"}, status_code=500)


@router.get("/api/export/weekly-pdf")
async def export_weekly_pdf(request: Request, week_start: str = "", db: AsyncSession = Depends(get_db)):
    try:
        profile_id = await get_profile_from_session(request)
        if not profile_id:
            return JSONResponse({"error": "not_authenticated"}, status_code=401)
        if not week_start:
            now = datetime.now(ZoneInfo(await get_profile_tz(profile_id, db)))
            week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        events, label = await _fetch_week_events(profile_id, week_start, db)
        day_map: dict = {}
        for e in events:
            day_map.setdefault(_fmt_day(e.get("occurred_at")), []).append(e)
        counts: dict = {}
        for e in events:
            src = e.get("source", "other")
            counts[src] = counts.get(src, 0) + 1
        summary_text = await _get_summary(profile_id, "weekly", week_start, db)
        week_stats   = await _fetch_week_stats(profile_id, week_start, db)
        from app.services.export_pdf import generate_weekly_pdf
        pdf_bytes = generate_weekly_pdf(label, summary_text, list(day_map.items()), counts, week_stats)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="weekly-{week_start}.pdf"'})
    except Exception as _exc:
        logger.exception("weekly-pdf failed: %s", _exc)
        return JSONResponse({"error": "export_failed"}, status_code=500)


def _events_to_csv(events: list) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "time", "source", "event_type", "workspace", "title"])
    for e in events:
        ts = e.get("occurred_at")
        if isinstance(ts, datetime):
            ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            date_str = ts.strftime("%Y-%m-%d")
            time_str = ts.strftime("%H:%M")
        else:
            date_str = time_str = ""
        writer.writerow([
            date_str, time_str,
            e.get("source", ""),
            e.get("event_type", ""),
            e.get("workspace", ""),
            e.get("title", ""),
        ])
    return output.getvalue()


@router.get("/api/export/daily-csv")
async def export_daily_csv(request: Request, date: str = "", db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if not date:
        date = datetime.now(ZoneInfo(await get_profile_tz(profile_id, db))).strftime("%Y-%m-%d")
    events, _ = await _fetch_day_events(profile_id, date, db)
    return Response(
        content=_events_to_csv(events),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="daily-{date}.csv"'},
    )


@router.get("/api/export/weekly-csv")
async def export_weekly_csv(request: Request, week_start: str = "", db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if not week_start:
        now = datetime.now(ZoneInfo(await get_profile_tz(profile_id, db)))
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    events, _ = await _fetch_week_events(profile_id, week_start, db)
    return Response(
        content=_events_to_csv(events),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="weekly-{week_start}.csv"'},
    )
