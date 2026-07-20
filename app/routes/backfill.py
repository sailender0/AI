"""On-demand event backfill. Self-only: always runs for the caller's own
profile, never a client-supplied one. See docs/adr-0003-backfill.md §5."""
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse

from app.auth.sso import require_profile
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
    profile_id: str = Depends(require_profile),
):
    if source not in SUPPORTED:
        return JSONResponse({"error": "unsupported_source"}, status_code=400)

    background_tasks.add_task(run_backfill, profile_id, source, days)
    return JSONResponse({"status": "accepted", "source": source, "days": days}, status_code=202)
