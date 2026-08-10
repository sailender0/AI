"""Report APIs.

- Attendance (`attendance_report` perm): a people × days grid, >=3 events/day = P.
  Row-scoped by role — user sees self, manager their reports, admin anyone; cross-user
  views/downloads audited.
- Consolidated (`consolidated_report` perm): a custom date-range, connector-filtered
  breakdown per day/week/month, for the caller or — for a manager — one of their
  direct reports. What it contains depends on three more admin-only permissions:
  `teams_activity` / `outlook_activity` widen the connector set, `activity_detail`
  turns counts into named events plus the AI narrative, `device_activity` adds focus
  time. Without them a viewer gets counts of GitHub, GitLab and Jira, and nothing else.
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import (
    granted, load_profile, report_target, visible_profiles,
)
from app.middleware.rate_limit import limiter
from app.services.attendance import attendance_csv, build_attendance
from app.services.consolidated import build_consolidated, summarize_consolidated
from app.services.export_pdf import generate_consolidated_pdf
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


# Chips an admin has to unlock. The rest are the base set every report holder gets.
_CHIP_PERM = {"teams": "teams_activity", "outlook": "outlook_activity"}


def permitted_chips(permissions) -> list[str]:
    """The connector chips `permissions` allows, in canonical order. The single
    place the connector gate is decided — the route clamps the request to this and
    the page renders exactly these, so the two can't drift."""
    held = set(permissions)
    return [c for c in _SOURCE_MAP if c not in _CHIP_PERM or _CHIP_PERM[c] in held]


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
    user_id: str | None = None             # None/self = own activity
    sources: list[str] | None = None
    event_types: list[str] | None = None
    detail: str = "brief"                   # brief | detail (narrative length)
    prompt: str | None = None


async def _run_consolidated(actor: Profile, db: AsyncSession, *, start: str, end: str,
                            user_id: str | None, sources: list[str] | None,
                            event_types: list[str] | None, depth: str,
                            prompt: str | None, action: str):
    """Authorize, build and narrate one consolidated report.

    The single place the permissions are applied, so the JSON view and the PDF
    download can't drift into showing different things. report_target() decides
    "may I?" and "for whom?" together and writes the cross-user audit row; the
    content permissions are read off the ACTOR, so they bite the same way whether
    they're looking at themselves or someone else.
    """
    target_id = await report_target("consolidated_report", "consolidated",
                                    user_id, actor, db, action=action)
    perms = granted(actor)

    allowed = permitted_chips(perms)
    chips = [c for c in (sources or allowed) if c in allowed] or allowed

    # Your own data is never reduced — the depth gate exists to protect the person
    # being looked at, and hiding your own event names from yourself protects nobody.
    is_self = target_id == str(actor.id)
    data = await build_consolidated(
        target_id, start, end, _expand(chips), event_types or [], db,
        detail=is_self or "activity_detail" in perms,
        device="device_activity" in perms,
    )

    try:
        summary = await summarize_consolidated(data, depth, prompt)
    except Exception as exc:
        logger.error("consolidated summary failed for %s: %s", target_id, exc)
        summary = ""

    return data, summary, chips, target_id, is_self


@router.post("/api/report/consolidated")
@limiter.limit("10/minute")
async def consolidated(request: Request, body: ConsolidatedReq,
                       actor: Profile = Depends(load_profile),
                       db: AsyncSession = Depends(get_db)):
    """Bucketed activity for the caller, or for one of their reports."""
    try:
        data, summary, chips, _, is_self = await _run_consolidated(
            actor, db, start=body.start, end=body.end, user_id=body.user_id,
            sources=body.sources, event_types=body.event_types,
            depth=body.detail, prompt=body.prompt, action="view",
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({
        "start": data["start"], "end": data["end"], "bucket": data["bucket"],
        "total": data["total"], "by_source": data["by_source"],
        "buckets": data["buckets"], "chips": chips,
        "detail": data["detail"], "device": data["device"],
        "truncated": data["truncated"], "summary": summary,
        "is_self": is_self,
    })


@router.get("/api/report/consolidated.pdf")
@limiter.limit("10/minute")
async def consolidated_pdf(request: Request, start: str = "", end: str = "",
                           user_id: str = "", sources: str = "", depth: str = "brief",
                           prompt: str = "",
                           actor: Profile = Depends(load_profile),
                           db: AsyncSession = Depends(get_db)):
    """The same report as a PDF. Rebuilt server-side rather than rendering whatever
    the client posts back, so the document can't disagree with the permissions."""
    if not is_date(start) or not is_date(end):
        return JSONResponse({"error": "invalid date"}, status_code=400)
    try:
        data, summary, _, target_id, is_self = await _run_consolidated(
            actor, db, start=start, end=end, user_id=user_id or None,
            sources=[s for s in sources.split(",") if s] or None, event_types=None,
            depth=depth, prompt=(prompt or None), action="download",
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    who = actor.email if is_self else (await db.get(Profile, uuid.UUID(target_id))).email
    pdf = generate_consolidated_pdf(
        who=who, start=data["start"], end=data["end"], bucket=data["bucket"],
        total=data["total"], by_source=data["by_source"], buckets=data["buckets"],
        summary=summary, detail=data["detail"], device=data["device"],
        truncated=data["truncated"],
    )
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="consolidated-{data["start"]}_{data["end"]}.pdf"'},
    )


@router.get("/api/report/consolidated/scope")
async def consolidated_scope(actor: Profile = Depends(load_profile),
                             db: AsyncSession = Depends(get_db)):
    """Who this caller may run the report for, and what it may contain — so the page
    renders the same set the API would accept instead of guessing."""
    perms = granted(actor)
    if "consolidated_report" not in perms:
        raise HTTPException(403, "forbidden")
    people = await visible_profiles(actor, db) if actor.role in ("manager", "admin") else [actor]
    return JSONResponse({
        "actor_id": str(actor.id),
        "chips": permitted_chips(perms),
        "detail": "activity_detail" in perms,
        "device": "device_activity" in perms,
        "people": [{"id": str(p.id), "email": p.email, "role": p.role} for p in people],
    })
