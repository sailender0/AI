"""Email an activity artifact to the signed-in user (self-only).

On-demand (POST /api/email/send), preview (POST /api/email/preview, for the
/email page), and scheduled digests (run_email_digest_job) share the
fetch → render → send pipeline. Delegated Graph Mail.Send — recipient is always
the user's own mailbox; `kind` selects the artifact and `date` its day/week.
"""
import logging
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import load_profile, report_target
from app.auth.sso import require_profile
from app.delivery.email_delivery import send_mail
from app.middleware.rate_limit import limiter
from app.services.email_report import render
from app.services.report_data import SUPPORTED_KINDS, fetch_report
from app.storage.models import EmailPreference, Profile
from app.storage.mongodb import access_log, email_sends
from app.storage.postgres import AsyncSessionLocal, get_db

router = APIRouter()
logger = logging.getLogger(__name__)

# Kinds a supervisor/admin may pull for ANOTHER user (privacy-reviewed subset).
_CROSS_USER_KINDS = {"my_day", "analytics"}
_FREQUENCIES = {"daily", "weekdays", "weekly"}


class EmailRequest(BaseModel):
    kind: str
    date: str | None = None       # YYYY-MM-DD local; None = today / current week
    user_id: str | None = None    # elevated only: whose report (report owner). None = self
    to_user_id: str | None = None # elevated only: recipient app user. None = the actor (self)


async def _run(kind: str, profile_id: str, to: str, date: str | None = None,
               sender_id: str | None = None) -> bool:
    """Fetch → render → send, in its own DB session. Returns True on a sent (202).
    `profile_id` owns the report data + is the recipient; `sender_id` owns the
    mailbox it's sent FROM (defaults to profile_id for self-service)."""
    try:
        async with AsyncSessionLocal() as db:
            data = await fetch_report(kind, profile_id, db, date)
        subject, html_body = render(kind, data)
        if await send_mail(sender_id or profile_id, to, subject, html_body):
            return True
        logger.warning("email '%s' not sent for %s", kind, profile_id)
    except Exception as exc:
        logger.error("email '%s' failed for %s: %s", kind, profile_id, exc)
    return False


async def _resolve_report_access(body: EmailRequest, actor: Profile, db: AsyncSession,
                                 action: str) -> str:
    """Target profile_id for an email request, authorized + audited.
    Self needs the email_report permission; a cross-user request (user_id set)
    needs supervisor/admin and a privacy-reviewed kind. Raises HTTPException,
    which FastAPI renders as {"detail": ...} with the right status code."""
    if body.user_id and body.user_id != str(actor.id) and body.kind not in _CROSS_USER_KINDS:
        raise HTTPException(400, "kind not allowed for another user")
    return await report_target("email_report", body.kind, body.user_id, actor, db, action)


async def _resolve_recipient(to_user_id: str | None, actor: Profile, owner_id: str,
                             kind: str, db: AsyncSession) -> Profile:
    """The Profile the mail is delivered to. Defaults to the actor (self/admin).
    Sending to anyone else is elevated-only and audited so the /email page stays
    self-only and every cross-user delivery is traceable."""
    if not to_user_id or to_user_id == str(actor.id):
        return actor
    if actor.role not in ("supervisor", "admin"):
        raise HTTPException(403, "forbidden")
    try:
        recip = await db.get(Profile, uuid.UUID(to_user_id))
    except ValueError:
        raise HTTPException(404, "no_such_recipient")
    if not recip:
        raise HTTPException(404, "no_such_recipient")
    try:
        await access_log().insert_one({
            "actor_profile_id":     str(actor.id),
            "actor_email":          actor.email,
            "report_owner_id":      owner_id,
            "recipient_profile_id": str(recip.id),
            "recipient_email":      recip.email,
            "kind":                 kind,
            "action":               "email_delivery",
            "at":                   datetime.now(timezone.utc),
        })
    except Exception as exc:
        logger.error("access_log delivery write failed (%s → %s by %s): %s",
                     owner_id, recip.id, actor.id, exc)
    return recip


@router.post("/api/email/preview")
@limiter.limit("20/minute")
async def preview_email(request: Request, body: EmailRequest,
                        actor: Profile = Depends(load_profile),
                        db: AsyncSession = Depends(get_db)):
    """Generate the report for {kind, date} and return {subject, html} WITHOUT
    sending, so the /email page (and admin per-user panel) can show it first."""
    if body.kind not in SUPPORTED_KINDS:
        return JSONResponse({"error": f"unsupported kind: {body.kind}"}, status_code=400)
    profile_id = await _resolve_report_access(body, actor, db, action="preview")
    try:
        data = await fetch_report(body.kind, profile_id, db, body.date)
        subject, html_body = render(body.kind, data)
    except Exception as exc:
        logger.error("preview '%s' failed for %s: %s", body.kind, profile_id, exc)
        return JSONResponse({"error": "preview_failed"}, status_code=502)
    return JSONResponse({"subject": subject, "html": html_body})


@router.post("/api/email/send")
@limiter.limit("5/minute")
async def send_email_report(
    request: Request,
    body: EmailRequest,
    background_tasks: BackgroundTasks,
    actor: Profile = Depends(load_profile),
    db: AsyncSession = Depends(get_db),
):
    if body.kind not in SUPPORTED_KINDS:
        return JSONResponse({"error": f"unsupported kind: {body.kind}"}, status_code=400)
    target_id = await _resolve_report_access(body, actor, db, action="email")

    if not await db.get(Profile, target_id):
        return JSONResponse({"error": "no_such_user"}, status_code=404)

    recipient = await _resolve_recipient(body.to_user_id, actor, target_id, body.kind, db)
    if not recipient.email:
        return JSONResponse({"error": "no_email_on_recipient"}, status_code=400)

    # Report DATA is target_id's; mail is DELIVERED to `recipient` (defaults to the
    # actor) and SENT FROM the actor's mailbox (delegated Graph token).
    background_tasks.add_task(_run, body.kind, target_id, recipient.email, body.date, str(actor.id))
    return JSONResponse({"status": "queued", "to": recipient.email})


# ── Scheduled digests ──────────────────────────────────────────────────────────

class PreferenceBody(BaseModel):
    kind: str
    frequency: str = "daily"   # daily | weekdays | weekly | off
    hour: int = 9              # local hour 0-23
    weekday: int = 4           # Fri (matches the app's weekly-summary cadence), used when weekly


@router.get("/api/email/preferences")
async def list_preferences(profile_id: str = Depends(require_profile),
                           db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(EmailPreference).where(EmailPreference.profile_id == profile_id)
    )).scalars().all()
    return JSONResponse({"preferences": [
        {"kind": r.kind, "frequency": r.frequency, "hour": r.hour,
         "weekday": r.weekday, "enabled": r.enabled}
        for r in rows
    ]})


@router.put("/api/email/preferences")
async def set_preference(body: PreferenceBody, profile_id: str = Depends(require_profile),
                         db: AsyncSession = Depends(get_db)):
    if body.kind not in SUPPORTED_KINDS:
        return JSONResponse({"error": f"unsupported kind: {body.kind}"}, status_code=400)
    if not (0 <= body.hour <= 23) or not (0 <= body.weekday <= 6):
        return JSONResponse({"error": "hour must be 0-23, weekday 0-6"}, status_code=400)

    enabled = body.frequency != "off"
    if enabled and body.frequency not in _FREQUENCIES:
        return JSONResponse({"error": f"bad frequency: {body.frequency}"}, status_code=400)
    freq = body.frequency if enabled else "daily"

    row = (await db.execute(
        select(EmailPreference).where(
            EmailPreference.profile_id == profile_id, EmailPreference.kind == body.kind
        )
    )).scalar_one_or_none()
    if row:
        row.frequency, row.hour, row.weekday, row.enabled = freq, body.hour, body.weekday, enabled
    else:
        db.add(EmailPreference(profile_id=profile_id, kind=body.kind, frequency=freq,
                               hour=body.hour, weekday=body.weekday, enabled=enabled))
    await db.commit()
    return JSONResponse({"ok": True, "enabled": enabled})


def _digest_due(frequency: str, hour: int, weekday: int, local_now: datetime) -> bool:
    """True when a digest with this schedule should fire at local_now (hour-granular)."""
    if local_now.hour != hour:
        return False
    if frequency == "weekdays":
        return local_now.weekday() < 5
    if frequency == "weekly":
        return local_now.weekday() == weekday
    return frequency == "daily"


async def run_email_digest_job():
    """APScheduler entry (hourly). Sends each enabled digest to its owner at their
    local scheduled hour (always the CURRENT day/week — no date param). Idempotent
    per (profile, kind, local date) via the email_sends unique index."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(EmailPreference, Profile)
            .join(Profile, Profile.id == EmailPreference.profile_id)
            .where(EmailPreference.enabled.is_(True))
        )).all()

    for pref, profile in rows:
        try:
            if not profile.email:
                continue
            local_now = datetime.now(ZoneInfo(profile.timezone or "UTC"))
            if not _digest_due(pref.frequency, pref.hour, pref.weekday, local_now):
                continue

            profile_id = str(pref.profile_id)
            date_str = local_now.strftime("%Y-%m-%d")
            try:
                await email_sends().insert_one({
                    "profile_id": profile_id, "kind": pref.kind,
                    "date": date_str, "sent_at": datetime.now(timezone.utc),
                })
            except DuplicateKeyError:
                continue  # already sent this kind today

            if await _run(pref.kind, profile_id, profile.email):
                logger.info("digest '%s' sent to %s", pref.kind, profile_id)
            else:
                await email_sends().delete_one(
                    {"profile_id": profile_id, "kind": pref.kind, "date": date_str}
                )
                logger.warning("digest '%s' send failed for %s", pref.kind, profile_id)
        except Exception as exc:
            logger.error("digest job error for %s: %s", getattr(pref, "kind", "?"), exc)
