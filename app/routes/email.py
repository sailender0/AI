"""Email an activity artifact to the signed-in user (self-only).

On-demand (POST /api/email/send), preview (POST /api/email/preview, for the
/email page), and scheduled digests (run_email_digest_job) share the
fetch → render → send pipeline. Delegated Graph Mail.Send — recipient is always
the user's own mailbox; `kind` selects the artifact and `date` its day/week.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import get_profile_from_session
from app.delivery.email_delivery import send_mail
from app.middleware.rate_limit import limiter
from app.services.email_report import render
from app.storage.models import EmailPreference, Profile
from app.storage.mongodb import email_sends
from app.storage.postgres import AsyncSessionLocal, get_db

router = APIRouter()
logger = logging.getLogger(__name__)

_SUPPORTED = {"standup", "device_activity", "device_activity_week", "analytics", "my_day"}
_FREQUENCIES = {"daily", "weekdays", "weekly"}


class EmailRequest(BaseModel):
    kind: str
    date: str | None = None   # YYYY-MM-DD local; None = today / current week


def _clamp_date(date: str | None, today: str) -> str:
    """A valid past-or-today YYYY-MM-DD, else today. Future dates clamp to today
    (string min works: ISO dates sort chronologically)."""
    if date and re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return min(date, today)
    return today


def _week_start_of(date_str: str) -> str:
    """Monday (YYYY-MM-DD) of the week containing date_str — the app's week def."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


async def _resolve_date(profile_id: str, db: AsyncSession, date: str | None):
    """(tzinfo, effective_local_date) — defaults to today, clamps future to today."""
    from app.services.activity_query import get_profile_tz
    from app.services.timezone import resolve, today_str
    tzinfo = resolve(await get_profile_tz(profile_id, db))
    return tzinfo, _clamp_date(date, today_str(tzinfo))


async def _fetch(kind: str, profile_id: str, db: AsyncSession, date: str | None = None) -> dict:
    # Lazy imports keep this route free of import cycles with the source routes.
    tzinfo, the_date = await _resolve_date(profile_id, db, date)

    if kind == "standup":
        from app.routes.standup import _generate
        return await _generate(profile_id, db, target_date=the_date)

    if kind == "device_activity":
        from app.routes.agent.analytics import build_activity_today
        data = await build_activity_today(profile_id, tzinfo, the_date)
        data["_date"] = the_date
        return data

    if kind == "device_activity_week":
        from app.routes.agent.analytics import build_activity_week
        from app.services.timezone import day_bounds, local_date
        from app.storage.mongodb import local_commits
        week_start = _week_start_of(the_date)
        week = await build_activity_week(profile_id, tzinfo, week_start)
        # per-day commit counts (the week payload only carries a total)
        w_start, _ = day_bounds(week_start, tzinfo)
        cdocs = await local_commits().find(
            {"profile_id": profile_id,
             "timestamp": {"$gte": w_start, "$lt": w_start + timedelta(days=7)}},
            projection={"timestamp": 1, "_id": 0},
        ).to_list(2000)
        by_day: dict[str, int] = {}
        for c in cdocs:
            ts = c.get("timestamp")
            if ts:
                dk = local_date(ts, tzinfo)
                by_day[dk] = by_day.get(dk, 0) + 1
        week["commits_by_day"] = by_day
        return week

    if kind == "analytics":
        from app.routes.exports import _fetch_week_stats, _fetch_week_events, _get_summary
        week_start = _week_start_of(the_date)
        events, _ = await _fetch_week_events(profile_id, week_start, db)
        return {
            "week_start": week_start,
            "stats": await _fetch_week_stats(profile_id, week_start, db),
            "summary": await _get_summary(profile_id, "weekly", week_start, db),
            "events": events,
        }

    if kind == "my_day":
        from app.routes.exports import _fetch_day_events, _get_summary
        events, _ = await _fetch_day_events(profile_id, the_date, db)
        counts: dict[str, int] = {}
        for e in events:
            s = e.get("source", "other")
            if s == "teams_subscription":
                s = "teams"
            counts[s] = counts.get(s, 0) + 1
        return {
            "date": the_date,
            "summary": await _get_summary(profile_id, "daily", the_date, db),
            "events": events,
            "counts": counts,
        }

    raise ValueError(f"unsupported kind: {kind}")


async def _run(kind: str, profile_id: str, to: str, date: str | None = None) -> bool:
    """Fetch → render → send, in its own DB session. Returns True on a sent (202)."""
    try:
        async with AsyncSessionLocal() as db:
            data = await _fetch(kind, profile_id, db, date)
        subject, html_body = render(kind, data)
        if await send_mail(profile_id, to, subject, html_body):
            return True
        logger.warning("email '%s' not sent for %s", kind, profile_id)
    except Exception as exc:
        logger.error("email '%s' failed for %s: %s", kind, profile_id, exc)
    return False


@router.post("/api/email/preview")
@limiter.limit("20/minute")
async def preview_email(request: Request, body: EmailRequest, db: AsyncSession = Depends(get_db)):
    """Generate the report for {kind, date} and return {subject, html} WITHOUT
    sending, so the /email page can show it before the user sends."""
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if body.kind not in _SUPPORTED:
        return JSONResponse({"error": f"unsupported kind: {body.kind}"}, status_code=400)
    try:
        data = await _fetch(body.kind, profile_id, db, body.date)
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
    db: AsyncSession = Depends(get_db),
):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if body.kind not in _SUPPORTED:
        return JSONResponse({"error": f"unsupported kind: {body.kind}"}, status_code=400)

    profile = await db.get(Profile, profile_id)
    if not profile or not profile.email:
        return JSONResponse({"error": "no_email_on_profile"}, status_code=400)

    # self-only: recipient is ALWAYS the signed-in user's own mailbox; server
    # regenerates from {kind, date} — no client-supplied body.
    background_tasks.add_task(_run, body.kind, profile_id, profile.email, body.date)
    return JSONResponse({"status": "queued", "to": profile.email})


# ── Scheduled digests ──────────────────────────────────────────────────────────

class PreferenceBody(BaseModel):
    kind: str
    frequency: str = "daily"   # daily | weekdays | weekly | off
    hour: int = 9              # local hour 0-23
    weekday: int = 4           # Fri (matches the app's weekly-summary cadence), used when weekly


@router.get("/api/email/preferences")
async def list_preferences(request: Request, db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    rows = (await db.execute(
        select(EmailPreference).where(EmailPreference.profile_id == profile_id)
    )).scalars().all()
    return JSONResponse({"preferences": [
        {"kind": r.kind, "frequency": r.frequency, "hour": r.hour,
         "weekday": r.weekday, "enabled": r.enabled}
        for r in rows
    ]})


@router.put("/api/email/preferences")
async def set_preference(request: Request, body: PreferenceBody, db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if body.kind not in _SUPPORTED:
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
