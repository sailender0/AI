"""FastAPI application entry point."""
import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.mongodb import init_indexes
from app.storage.postgres import get_db
from app.storage.redis_client import close_redis

from app.auth.sso import router as sso_router, get_profile_from_session
from app.auth.oauth import router as oauth_router
from app.auth.github_app import router as github_app_router
from app.routes.pages import router as pages_router
from app.routes.profile import router as profile_router
from app.routes.activity import router as activity_router
from app.routes.stats import router as stats_router
from app.routes.summaries import router as summaries_router
from app.routes.exports import router as exports_router
from app.webhooks.receivers.teams import router as teams_router
from app.webhooks.receivers.github import router as github_router
from app.webhooks.receivers.gitlab import router as gitlab_router
from app.webhooks.receivers.jira import router as jira_router
from app.ai.query import router as query_router
from app.routes.agent import router as agent_router
from app.routes.standup import router as standup_router, run_standup_job

from app.webhooks.renewal import (
    check_github_webhook_health,
    renew_jira_webhooks,
    renew_teams_subscriptions,
)
from app.ai.summarizer import run_summary_job, run_startup_catchup
from app.middleware.rate_limit import limiter
from app.middleware.request_id import RequestIDMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_indexes()

    scheduler.add_job(renew_teams_subscriptions, "interval", minutes=45, id="teams_renewal")
    scheduler.add_job(renew_jira_webhooks, "interval", days=20, id="jira_renewal")
    scheduler.add_job(check_github_webhook_health, "interval", hours=6, id="github_health")
    # Hourly: run_summary_job generates only for profiles whose LOCAL hour is 23, so
    # every timezone gets its daily summary at its own 23:00. (docs/adr-0001-timezone.md)
    scheduler.add_job(run_summary_job, "cron", minute=59, args=["daily"], id="daily_summary")
    # Hourly: generates only for profiles whose LOCAL time is Friday 17:00, so every
    # timezone gets its weekly summary at its own Friday evening. (docs/adr-0001-timezone.md)
    scheduler.add_job(run_summary_job, "cron", minute=0, args=["weekly"], id="weekly_summary")
    # Hourly: generates + flags the standup for profiles at their local 09:00, for
    # proactive delivery via the desktop agent. (docs/adr-0002-delivery.md)
    scheduler.add_job(run_standup_job, "cron", minute=30, id="standup_job")

    scheduler.start()
    logger.info("Scheduler started")

    _catchup_task = asyncio.create_task(run_startup_catchup())

    def _log_catchup_error(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            logger.error("run_startup_catchup failed: %s", t.exception(), exc_info=t.exception())

    _catchup_task.add_done_callback(_log_catchup_error)

    yield

    scheduler.shutdown()
    await close_redis()


app = FastAPI(
    title="Developer Activity Tracker",
    lifespan=lifespan,
    swagger_ui_parameters={"withCredentials": True},
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(RequestIDMiddleware)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(pages_router)
app.include_router(profile_router)
app.include_router(activity_router)
app.include_router(stats_router)
app.include_router(summaries_router)
app.include_router(exports_router)
app.include_router(sso_router)
app.include_router(oauth_router)
app.include_router(github_app_router)
app.include_router(teams_router)
app.include_router(github_router)
app.include_router(gitlab_router)
app.include_router(jira_router)
app.include_router(query_router)
app.include_router(agent_router)
app.include_router(standup_router)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.post("/setup/github-app")
async def setup_github_app(request: Request, db: AsyncSession = Depends(get_db)):
    """One-time: maps your GitHub App installation ID to your logged-in profile."""
    from app.config import settings
    from app.storage.models import LinkedIdentity

    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    if not settings.GITHUB_APP_INSTALLATION_ID:
        return JSONResponse({"error": "GITHUB_APP_INSTALLATION_ID not set in .env"}, status_code=400)

    conflict = (await db.execute(
        select(LinkedIdentity).where(
            LinkedIdentity.provider == "github",
            LinkedIdentity.tenant_id == settings.GITHUB_APP_INSTALLATION_ID,
            LinkedIdentity.profile_id != profile_id,
        )
    )).scalar_one_or_none()
    if conflict:
        return JSONResponse({"error": "installation_already_claimed"}, status_code=409)

    existing = (await db.execute(
        select(LinkedIdentity).where(
            LinkedIdentity.profile_id == profile_id,
            LinkedIdentity.provider == "github",
        )
    )).scalar_one_or_none()

    if existing:
        existing.tenant_id = settings.GITHUB_APP_INSTALLATION_ID
    else:
        db.add(LinkedIdentity(
            profile_id=profile_id,
            provider="github",
            tenant_id=settings.GITHUB_APP_INSTALLATION_ID,
            workspace_label=settings.GITHUB_ORG,
        ))
    await db.commit()

    return JSONResponse({
        "status": "ok",
        "installation_id": settings.GITHUB_APP_INSTALLATION_ID,
        "profile_id": profile_id,
    })
