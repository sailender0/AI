"""Report APIs.

- Attendance (`attendance_report` perm): a people × days grid, >=3 events/day = P.
  Row-scoped by role — user sees self, manager their reports, admin anyone; cross-user
  views/downloads audited.
"""
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import granted, load_profile, visible_profiles
from app.middleware.rate_limit import limiter
from app.services.attendance import attendance_csv, build_attendance
from app.storage.models import Profile
from app.storage.mongodb import access_log
from app.storage.postgres import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# UI sends friendly names; "teams" maps to the stored source.
_SOURCE_MAP = {"github": "github", "gitlab": "gitlab", "jira": "jira",
               "teams": "teams_subscription", "teams_subscription": "teams_subscription"}


class AttendanceReq(BaseModel):
    start: str
    end: str
    sources: list[str] | None = None       # empty/None = all connectors
    user_ids: list[str] | None = None      # narrow to a subset (clamped to scope)


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
    sources = [_SOURCE_MAP[s] for s in (body.sources or []) if s in _SOURCE_MAP]
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
    if not _DATE_RE.match(start or "") or not _DATE_RE.match(end or ""):
        return JSONResponse({"error": "invalid date"}, status_code=400)
    user_ids = [u for u in users.split(",") if u] or None
    scoped = await _attendance_scope(actor, user_ids, db)
    if not scoped:
        return JSONResponse({"error": "no users in scope"}, status_code=400)
    src = [_SOURCE_MAP[s] for s in sources.split(",") if s in _SOURCE_MAP]
    try:
        data = await build_attendance(scoped, start, end, src, actor.timezone or "UTC")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    await _audit_attendance(actor, scoped, "download")
    return Response(
        content=attendance_csv(data), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="attendance-{start}_{end}.csv"'},
    )
