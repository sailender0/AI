from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import get_profile_from_session, require_profile
from app.config import settings
from app.services.activity_query import get_integrations
from app.storage.models import Profile
from app.storage.postgres import get_db

router = APIRouter()


@router.get("/api/me")
async def get_me(request: Request, db: AsyncSession = Depends(get_db)):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"authenticated": False})
    profile = await db.get(Profile, profile_id)
    if not profile:
        return JSONResponse({"authenticated": False})
    integrations, integration_errors = await get_integrations(profile_id, db)
    from app.auth.rbac import granted
    return JSONResponse({
        "authenticated": True,
        "email": profile.email,
        "profile_id": str(profile.id),
        "role": profile.role,
        "permissions": granted(profile),
        "integrations": integrations,
        "integration_errors": integration_errors,
        "connect_urls": {
            "github": f"https://github.com/apps/{settings.GITHUB_APP_SLUG}/installations/new",
            "gitlab": f"{settings.APP_BASE_URL}/connect/gitlab",
            "jira": f"{settings.APP_BASE_URL}/connect/jira",
            "teams_subscription": None,
        },
    })


@router.patch("/api/profile/timezone")
async def update_profile_timezone(request: Request, profile_id: str = Depends(require_profile),
                                  db: AsyncSession = Depends(get_db)):
    body = await request.json()
    tz_name = body.get("timezone", "UTC")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return JSONResponse({"error": "invalid timezone"}, status_code=400)
    profile = await db.get(Profile, profile_id)
    if profile:
        profile.timezone = tz_name
        await db.commit()
    return JSONResponse({"ok": True, "timezone": tz_name})
