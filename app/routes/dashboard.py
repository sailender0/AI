import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.models import Integration, LinkedIdentity, Profile
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

_SOURCES = ["github", "gitlab", "jira", "teams_subscription"]
_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "jira": "Jira",
    "teams_subscription": "Teams",
}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    profile_id = await get_profile_from_session(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "logged_in": profile_id is not None,
            "github_app_slug": settings.GITHUB_APP_SLUG,
            "app_base_url": settings.APP_BASE_URL,
        },
    )


@router.get("/api/me")
async def get_me(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"authenticated": False})

    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
        if not profile:
            return JSONResponse({"authenticated": False})

        rows = (
            await db.execute(
                select(Integration).where(Integration.profile_id == profile_id)
            )
        ).scalars().all()

        linked = (
            await db.execute(
                select(LinkedIdentity).where(LinkedIdentity.profile_id == profile_id)
            )
        ).scalars().all()

    connected = {r.source for r in rows if r.sync_status == "active"}
    linked_providers = {l.provider for l in linked}

    integrations = []
    for source in ["github", "gitlab", "jira", "teams_subscription"]:
        is_connected = source in connected or (
            source == "github" and "github" in linked_providers
        )
        integrations.append({
            "source": source,
            "label": _LABELS[source],
            "connected": is_connected,
        })

    return JSONResponse({
        "authenticated": True,
        "email": profile.email,
        "profile_id": str(profile.id),
        "integrations": integrations,
    })


@router.get("/api/events/recent")
async def get_recent_events(request: Request, limit: int = 20):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    events = (
        await activity_events()
        .find({"profile_id": profile_id}, {"raw_payload": 0})
        .sort("occurred_at", -1)
        .to_list(length=limit)
    )

    result = []
    for e in events:
        ts = e.get("occurred_at")
        result.append({
            "id": str(e.get("_id", "")),
            "source": e.get("source", ""),
            "event_type": e.get("event_type", ""),
            "title": e.get("title", ""),
            "workspace": e.get("workspace", ""),
            "occurred_at": ts.isoformat() if isinstance(ts, datetime) else str(ts),
        })

    return JSONResponse({"events": result})
