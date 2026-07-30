import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import load_profile, report_target
from app.services.activity_query import get_profile_tz
from app.services.report_data import (
    fetch_day_events, fetch_week_events, fetch_week_stats, get_summary,
)
from app.services.timezone import resolve, today_str
from app.storage.models import Profile
from app.storage.postgres import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INVALID_DATE = {"error": "invalid date"}


def _attachment(filename: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


def _valid_date(s: str) -> bool:
    """Empty (defaulted downstream) or a YYYY-MM-DD string. Keeps a user value out
    of both the Mongo query and the Content-Disposition filename unvalidated."""
    return not s or bool(_DATE_RE.match(s))


def _csv_safe(v) -> str:
    """Neutralize spreadsheet formula injection: a cell that starts with =, +, -,
    @ or a control char is evaluated by Excel/Sheets on open. Titles and workspace
    names come from webhook payloads, so prefix those with a quote to force text."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _fmt_day(dt) -> str:
    if isinstance(dt, datetime):
        return f"{dt.strftime('%A, %b')} {dt.day}"
    return "Unknown"


@router.get("/api/export/daily-pdf")
async def export_daily_pdf(date: str = "", user_id: str = "",
                           actor: Profile = Depends(load_profile),
                           db: AsyncSession = Depends(get_db)):
    if not _valid_date(date):
        return JSONResponse(_INVALID_DATE, status_code=400)
    profile_id = await report_target("export_my_day", "my_day", user_id, actor, db)
    try:
        if not date:
            date = today_str(resolve(await get_profile_tz(profile_id, db)))
        events, label = await fetch_day_events(profile_id, date, db)
        summary_text  = await get_summary(profile_id, "daily", date, db)
        from app.services.export_pdf import generate_daily_pdf
        pdf_bytes = generate_daily_pdf(label, summary_text, events)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers=_attachment(f"daily-{date}.pdf"))
    except Exception as _exc:
        logger.exception("daily-pdf failed: %s", _exc)
        return JSONResponse({"error": "export_failed"}, status_code=500)


@router.get("/api/export/weekly-pdf")
async def export_weekly_pdf(week_start: str = "", user_id: str = "",
                            actor: Profile = Depends(load_profile),
                            db: AsyncSession = Depends(get_db)):
    if not _valid_date(week_start):
        return JSONResponse(_INVALID_DATE, status_code=400)
    profile_id = await report_target("export_analytics", "analytics", user_id, actor, db)
    try:
        if not week_start:
            now = datetime.now(resolve(await get_profile_tz(profile_id, db)))
            week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        events, label = await fetch_week_events(profile_id, week_start, db)
        day_map: dict = {}
        for e in events:
            day_map.setdefault(_fmt_day(e.get("occurred_at")), []).append(e)
        counts: dict = {}
        for e in events:
            src = e.get("source", "other")
            counts[src] = counts.get(src, 0) + 1
        summary_text = await get_summary(profile_id, "weekly", week_start, db)
        week_stats   = await fetch_week_stats(profile_id, week_start, db)
        from app.services.export_pdf import generate_weekly_pdf
        pdf_bytes = generate_weekly_pdf(label, summary_text, list(day_map.items()), counts, week_stats)
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers=_attachment(f"weekly-{week_start}.pdf"))
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
            _csv_safe(e.get("source", "")),
            _csv_safe(e.get("event_type", "")),
            _csv_safe(e.get("workspace", "")),
            _csv_safe(e.get("title", "")),
        ])
    return output.getvalue()


@router.get("/api/export/daily-csv")
async def export_daily_csv(date: str = "", user_id: str = "",
                           actor: Profile = Depends(load_profile),
                           db: AsyncSession = Depends(get_db)):
    if not _valid_date(date):
        return JSONResponse(_INVALID_DATE, status_code=400)
    profile_id = await report_target("export_my_day", "my_day", user_id, actor, db)
    if not date:
        date = today_str(resolve(await get_profile_tz(profile_id, db)))
    events, _ = await fetch_day_events(profile_id, date, db)
    return Response(
        content=_events_to_csv(events),
        media_type="text/csv",
        headers=_attachment(f"daily-{date}.csv"),
    )


@router.get("/api/export/weekly-csv")
async def export_weekly_csv(week_start: str = "", user_id: str = "",
                            actor: Profile = Depends(load_profile),
                            db: AsyncSession = Depends(get_db)):
    if not _valid_date(week_start):
        return JSONResponse(_INVALID_DATE, status_code=400)
    profile_id = await report_target("export_analytics", "analytics", user_id, actor, db)
    if not week_start:
        now = datetime.now(resolve(await get_profile_tz(profile_id, db)))
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    events, _ = await fetch_week_events(profile_id, week_start, db)
    return Response(
        content=_events_to_csv(events),
        media_type="text/csv",
        headers=_attachment(f"weekly-{week_start}.csv"),
    )
