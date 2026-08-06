"""Report APIs.

- Attendance (`attendance_report` perm): a people × days grid, >=3 events/day = P.
  Row-scoped by role — user sees self, manager their reports, admin anyone; cross-user
  views/downloads audited.
- Consolidated (`consolidated_report` perm): a custom date-range, connector-filtered,
  AI-summarised brief/detailed narrative. Self-service — the caller's own activity.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import granted, load_profile, require_permission, visible_profiles
from app.middleware.rate_limit import limiter
from app.services.attendance import attendance_csv, build_attendance
from app.services.consolidated import build_consolidated, summarize_consolidated
from app.services.timezone import is_date
from app.storage.models import Profile
from app.storage.mongodb import access_log
from app.storage.postgres import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

_SOURCE_MAP = {
    "github":  ["github"],
    "gitlab":  ["gitlab"],
    "jira":    ["jira"],
    "teams":   ["teams_chat", "teams_call"],
    "outlook": ["outlook_mail", "outlook_calendar"],
}


def _expand(chips) -> list[str]:
    """Chip names → stored source values. Unknown chips are dropped; an empty
    result means "no source filter" (every caller decides what that implies)."""
    return [s for c in chips or [] if c in _SOURCE_MAP for s in _SOURCE_MAP[c]]


class AttendanceReq(BaseModel):
    start: str
    end: str
    sources: list[str] | None = None
    user_ids: list[str] | None = None


async def _attendance_scope(actor: Profile, user_ids, db) -> list[Profile]:
    """Row-scope for the caller, then optionally narrow to selected ids — clamped so
    a client can never request someone outside their allowed set."""
    if "attendance_report" not in granted(actor):
        raise HTTPException(403, "forbidden")
    scoped = await visible_profiles(actor, db)
    if user_ids:
        wanted = set(user_ids)
        scoped = [p for p in scoped if str(p.id) in wanted]
    return scoped


async def _audit_attendance(actor: Profile, scoped: list[Profile], action: str) -> None:
    """Audit a cross-user attendance view/download (self-only views aren't logged)."""
    others = [p for p in scoped if str(p.id) != str(actor.id)]
    if not others:
        return
    try:
        await access_log().insert_one({
            "actor_profile_id": str(actor.id), "actor_email": actor.email,
            "actor_role": actor.role, "kind": "attendance", "action": action,
            "target_count": len(others),
            "target_ids": [str(p.id) for p in others][:100],
            "at": datetime.now(timezone.utc),
        })
    except Exception as exc:
        logger.error("attendance audit write failed for %s: %s", actor.id, exc)


@router.post("/api/report/attendance")
@limiter.limit("10/minute")
async def attendance(request: Request, body: AttendanceReq,
                     actor: Profile = Depends(load_profile),
                     db: AsyncSession = Depends(get_db)):
    scoped = await _attendance_scope(actor, body.user_ids, db)
    if not scoped:
        return JSONResponse({"error": "no users in scope"}, status_code=400)
    sources = _expand(body.sources)
    try:
        data = await build_attendance(scoped, body.start, body.end, sources,
                                      actor.timezone or "UTC")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    await _audit_attendance(actor, scoped, "view")
    return JSONResponse(data)


@router.get("/api/report/attendance.csv")
async def attendance_download(request: Request, start: str = "", end: str = "",
                              sources: str = "", users: str = "",
                              actor: Profile = Depends(load_profile),
                              db: AsyncSession = Depends(get_db)):
    if not is_date(start) or not is_date(end):
        return JSONResponse({"error": "invalid date"}, status_code=400)
    user_ids = [u for u in users.split(",") if u] or None
    scoped = await _attendance_scope(actor, user_ids, db)
    if not scoped:
        return JSONResponse({"error": "no users in scope"}, status_code=400)
    src = _expand(sources.split(","))
    try:
        data = await build_attendance(scoped, start, end, src, actor.timezone or "UTC")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    await _audit_attendance(actor, scoped, "download")
    return Response(
        content=attendance_csv(data), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="attendance-{start}_{end}.csv"'},
    )


class ConsolidatedReq(BaseModel):
    start: str
    end: str
    sources: list[str] | None = None
    event_types: list[str] | None = None
    detail: str = "brief"
    prompt: str | None = None


@router.post("/api/report/consolidated")
@limiter.limit("10/minute")
async def consolidated(request: Request, body: ConsolidatedReq,
                       profile_id: str = Depends(require_permission("consolidated_report")),
                       db: AsyncSession = Depends(get_db)):
    """AI narrative over a custom date range — always the caller's own activity."""
    sources = _expand(body.sources) or _expand(_SOURCE_MAP)
    try:
        data = await build_consolidated(
            profile_id, body.start, body.end, sources, body.event_types or [], db,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        summary = await summarize_consolidated(data, body.detail, body.prompt)
    except Exception as exc:
        logger.error("consolidated summary failed for %s: %s", profile_id, exc)
        summary = ""

    return JSONResponse({
        "start": data["start"], "end": data["end"],
        "total": data["total"], "by_source": data["by_source"],
        "truncated": data["truncated"], "summary": summary,
    })
