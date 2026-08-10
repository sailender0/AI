"""Activity calendar endpoints — HTTP only.

The month/day shaping lives in app/services/calendar_activity.py; these handlers
validate the request, resolve the caller's timezone, and return the result.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import require_profile
from app.services.activity_query import get_profile_tz
from app.services.calendar_activity import build_day, build_month
from app.storage.postgres import get_db

router = APIRouter()

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


@router.get("/api/calendar/month")
async def calendar_month(month: str = Query(...), person: str | None = None,
                         profile_id: str = Depends(require_profile),
                         db: AsyncSession = Depends(get_db)):
    """Dot counts per day, month totals, and the people seen this month."""
    if not _MONTH.match(month):
        raise HTTPException(400, "month must be YYYY-MM")
    tz = await get_profile_tz(profile_id, db)
    return JSONResponse(await build_month(profile_id, month, tz, person))


@router.get("/api/calendar/day")
async def calendar_day(date: str = Query(...), person: str | None = None,
                       profile_id: str = Depends(require_profile),
                       db: AsyncSession = Depends(get_db)):
    """One local day's timeline, oldest first."""
    if not _DATE.match(date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    tz = await get_profile_tz(profile_id, db)
    return JSONResponse(await build_day(profile_id, date, tz, person))
