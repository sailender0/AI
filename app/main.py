"""
FastAPI application entry point.
Mounts all routers, starts APScheduler background jobs, and initialises storage.
"""
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.storage.mongodb import init_indexes
from app.storage.postgres import init_db
from app.storage.redis_client import close_redis

from app.auth.sso import router as sso_router
from app.auth.oauth import router as oauth_router
from app.auth.github_app import router as github_app_router
from app.routes.dashboard import router as dashboard_router
from app.webhooks.receivers.teams import router as teams_router
from app.webhooks.receivers.github import router as github_router
from app.webhooks.receivers.gitlab import router as gitlab_router
from app.webhooks.receivers.jira import router as jira_router
from app.ai.query import router as query_router

from app.webhooks.renewal import (
    check_github_webhook_health,
    renew_jira_webhooks,
    renew_teams_subscriptions,
)
from app.ai.summarizer import run_summary_job, run_startup_catchup
from app.middleware.rate_limit import limiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_indexes()

    # Teams: every 45 minutes
    scheduler.add_job(renew_teams_subscriptions, "interval", minutes=45, id="teams_renewal")
    # Jira: every 20 days
    scheduler.add_job(renew_jira_webhooks, "interval", days=20, id="jira_renewal")
    # GitHub: health check every 6 hours
    scheduler.add_job(check_github_webhook_health, "interval", hours=6, id="github_health")
    # Daily summary: fires every hour at :59; summarizer skips users not yet at local 23:xx
    scheduler.add_job(run_summary_job, "cron", minute=59, args=["daily"], id="daily_summary")
    # Weekly summary: 5 PM every Friday
    scheduler.add_job(run_summary_job, "cron", day_of_week="fri", hour=17, minute=0, args=["weekly"], id="weekly_summary")

    scheduler.start()
    logger.info("Scheduler started")

    # Generate any daily/weekly summaries missed while the app was offline
    import asyncio
    asyncio.create_task(run_startup_catchup())

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

app.include_router(dashboard_router)
app.include_router(sso_router)
app.include_router(oauth_router)
app.include_router(github_app_router)
app.include_router(teams_router)
app.include_router(github_router)
app.include_router(gitlab_router)
app.include_router(jira_router)
app.include_router(query_router)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.post("/setup/github-app")
async def setup_github_app(request: Request):
    """One-time: maps your GitHub App installation ID to your logged-in profile."""
    from app.auth.sso import get_profile_from_session
    from app.config import settings
    from app.storage.models import LinkedIdentity
    from app.storage.postgres import AsyncSessionLocal
    from sqlalchemy import select

    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    if not settings.GITHUB_APP_INSTALLATION_ID:
        return JSONResponse({"error": "GITHUB_APP_INSTALLATION_ID not set in .env"}, status_code=400)

    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(
                select(LinkedIdentity).where(
                    LinkedIdentity.profile_id == profile_id,
                    LinkedIdentity.provider == "github",
                )
            )
        ).scalar_one_or_none()

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


