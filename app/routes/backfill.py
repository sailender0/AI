"""On-demand event backfill. Self-only: always runs for the caller's own
profile, never a client-supplied one. See docs/adr-0003-backfill.md §5."""
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app.auth.sso import get_profile_from_session
from app.backfill.runner import SUPPORTED, run_backfill
from app.middleware.rate_limit import limiter

router = APIRouter()


@router.post("/api/backfill/{source}")
@limiter.limit("2/hour")
async def trigger_backfill(
    source: str,
    request: Request,
    background_tasks: BackgroundTasks,
    days: int = 30,
):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if source not in SUPPORTED:
        return JSONResponse({"error": "unsupported_source"}, status_code=400)

    background_tasks.add_task(run_backfill, profile_id, source, days)
    return JSONResponse({"status": "accepted", "source": source, "days": days}, status_code=202)
