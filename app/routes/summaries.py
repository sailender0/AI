import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.summarizer import _summarise_profile
from app.auth.sso import require_profile
from app.storage.models import Profile, Summary
from app.storage.postgres import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/summaries")
async def get_summaries(limit: int = 10, profile_id: str = Depends(require_profile),
                        db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Summary)
        .where(Summary.profile_id == profile_id)
        .order_by(Summary.period_end.desc())
        .limit(limit)
    )).scalars().all()

    return JSONResponse({"summaries": [
        {
            "id": str(s.id),
            "period_type": s.period_type,
            "period_start": s.period_start.isoformat() if s.period_start else None,
            "period_end": s.period_end.isoformat() if s.period_end else None,
            "content": s.content,
        }
        for s in rows
    ]})


@router.post("/api/summaries/generate")
async def generate_summary(request: Request, profile_id: str = Depends(require_profile),
                           db: AsyncSession = Depends(get_db)):
    body = await request.json()
    period_type = body.get("period_type", "daily")
    specific_date = body.get("date")
    if period_type not in ("daily", "weekly"):
        return JSONResponse({"error": "invalid period_type"}, status_code=400)

    profile = await db.get(Profile, profile_id)
    if not profile:
        return JSONResponse({"error": "profile_not_found"}, status_code=404)

    try:
        await _summarise_profile(profile, profile_id, period_type, full_day=True, specific_date=specific_date)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("On-demand summary failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.delete("/api/summaries/{summary_id}")
async def delete_summary(summary_id: str, profile_id: str = Depends(require_profile),
                         db: AsyncSession = Depends(get_db)):
    import uuid as _uuid
    row = await db.get(Summary, _uuid.UUID(summary_id))
    if not row or str(row.profile_id) != str(profile_id):
        return JSONResponse({"error": "not_found"}, status_code=404)
    await db.delete(row)
    await db.commit()
    return JSONResponse({"ok": True})
