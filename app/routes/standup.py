"""Standup endpoints — HTTP only, plus the hourly scheduler entry point.

Generation lives in app/services/standup.py so the email report can reuse it.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.summarizer import _is_scheduled_time
from app.auth.sso import require_profile
from app.services.activity_query import get_profile_tz
from app.services.standup import generate, yesterday_date
from app.storage.models import Profile
from app.storage.mongodb import standups
from app.storage.postgres import AsyncSessionLocal, get_db

router = APIRouter()
log    = logging.getLogger(__name__)


@router.get("/api/standup/today")
async def get_standup(profile_id: str = Depends(require_profile),
                      db: AsyncSession = Depends(get_db)):
    try:
        return JSONResponse(await generate(profile_id, db))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.post("/api/standup/regenerate")
async def regenerate_standup(profile_id: str = Depends(require_profile),
                             db: AsyncSession = Depends(get_db)):
    tz_name  = await get_profile_tz(profile_id, db)
    date_str = yesterday_date(tz_name)
    await standups().delete_one({"profile_id": profile_id, "date": date_str})
    try:
        return JSONResponse(await generate(profile_id, db))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/standup/date/{date_str}")
async def get_standup_by_date(date_str: str, profile_id: str = Depends(require_profile),
                              db: AsyncSession = Depends(get_db)):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"error": "Use YYYY-MM-DD format"}, status_code=400)
    try:
        return JSONResponse(await generate(profile_id, db, target_date=date_str))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/api/standup/history")
async def get_standup_history(profile_id: str = Depends(require_profile)):
    docs = await standups().find(
        {"profile_id": profile_id},
        projection={"date": 1, "text": 1, "generated_at": 1, "_id": 0},
    ).sort("date", -1).to_list(30)
    return JSONResponse({"standups": [
        {
            "date": d["date"],
            "text": d["text"],
            "generated_at": d["generated_at"].isoformat() if d.get("generated_at") else None,
        }
        for d in docs
    ]})


async def run_standup_job():
    """APScheduler entry point (hourly). For each profile whose LOCAL time is the
    standup hour, generate yesterday's standup and flag it for proactive delivery.
    The agent picks up delivery_pending docs (docs/adr-0002-delivery.md)."""
    async with AsyncSessionLocal() as db:
        profiles = (await db.execute(select(Profile))).scalars().all()

    for profile in profiles:
        profile_id = str(profile.id)
        try:
            local_now = datetime.now(ZoneInfo(profile.timezone or "UTC"))
            if not _is_scheduled_time("standup", local_now):
                continue
            async with AsyncSessionLocal() as db:
                result = await generate(profile_id, db)
            await standups().update_one(
                {"profile_id": profile_id, "date": result["date"]},
                {"$set": {"delivery_pending": True}},
            )
            log.info("Standup job: generated + flagged for %s (%s)", profile_id, result["date"])
        except Exception as exc:
            log.error("Standup job failed for %s: %s", profile_id, exc)
