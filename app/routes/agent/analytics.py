"""My Activity analytics endpoints — HTTP only.

The focus/AI-tool/token computation lives in app/services/device_analytics.py so
the AI layer and the email reports can build the same payloads directly.
"""
import re
from datetime import timedelta

from fastapi import APIRouter, Depends

from app.auth.sso import require_profile
from app.services.device_analytics import (
    build_activity_today, build_activity_week, period_ranges, token_total,
)
from app.services.timezone import now_local, resolve, today_str

router = APIRouter()


@router.get("/today")
async def activity_today(
    date: str = "",
    tz: str = "",
    device_id: str = "",
    profile_id: str = Depends(require_profile),
):
    tzinfo   = resolve(request_tz=tz)
    the_date = date if (date and re.match(r"^\d{4}-\d{2}-\d{2}$", date)) else today_str(tzinfo)
    return await build_activity_today(profile_id, tzinfo, the_date, device_id)


@router.get("/week")
async def activity_week(tz: str = "", week_start: str = "",
                        profile_id: str = Depends(require_profile)):
    tzinfo = resolve(request_tz=tz)

    if week_start and re.match(r"^\d{4}-\d{2}-\d{2}$", week_start):
        week_start_str = week_start
    else:
        # Default: Monday of the current local week (not a rolling 7 days)
        local_now      = now_local(tzinfo)
        week_start_str = (local_now - timedelta(days=local_now.weekday())).strftime("%Y-%m-%d")
    return await build_activity_week(profile_id, tzinfo, week_start_str)


@router.get("/token-comparison")
async def token_comparison(granularity: str = "week", tz: str = "",
                           profile_id: str = Depends(require_profile)):
    """Claude token totals for the current vs. previous week/month — powers the
    My Activity comparison chart and the chat 'this week vs last week' answer."""
    tzinfo = resolve(request_tz=tz)
    gran   = "month" if granularity == "month" else "week"
    rng    = period_ranges(gran, now_local(tzinfo).date())
    (tf, tt), (lf, lt) = rng["this"], rng["last"]
    return {
        "granularity": gran,
        "this": {"from": tf, "to": tt, "total": await token_total(profile_id, tf, tt)},
        "last": {"from": lf, "to": lt, "total": await token_total(profile_id, lf, lt)},
    }
