"""
GitHub App installation callback.
GitHub redirects here after a user installs / updates / uninstalls the App.
No manual setup needed — installation_id is mapped to the logged-in profile automatically.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, delete

from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.models import Integration, LinkedIdentity
from app.storage.postgres import AsyncSessionLocal
from app.storage.redis_client import get_redis

router = APIRouter()
logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_gh_install:{state}"


@router.get("/github/app/callback")
async def github_app_callback(
    request: Request,
    installation_id: str = "",
    setup_action: str = "install",
    state: str = "",
):
    profile_id = await get_profile_from_session(request)

    if not profile_id:
        # Not logged in — save the installation details and send to login first
        redis = get_redis()
        import secrets
        pending_state = secrets.token_urlsafe(16)
        await redis.set(
            _PENDING_KEY.format(state=pending_state),
            f"{installation_id}:{setup_action}",
            ex=600,
        )
        return RedirectResponse(
            url=f"/auth/login?next=/github/app/resume?state={pending_state}"
        )

    return await _handle_installation(profile_id, installation_id, setup_action)


@router.get("/github/app/resume")
async def github_app_resume(request: Request, state: str = ""):
    """Called after login when user wasn't authenticated during App installation."""
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    redis = get_redis()
    pending = await redis.get(_PENDING_KEY.format(state=state))
    if not pending:
        return JSONResponse({"error": "installation session expired"}, status_code=400)

    await redis.delete(_PENDING_KEY.format(state=state))
    installation_id, setup_action = pending.split(":", 1)
    return await _handle_installation(profile_id, installation_id, setup_action)


async def _handle_installation(
    profile_id: str, installation_id: str, setup_action: str
):
    if setup_action == "deleted":
        await _remove_installation(profile_id, installation_id)
        logger.info("GitHub App uninstalled for profile %s", profile_id)
        return RedirectResponse(url="/?github=disconnected")

    await _upsert_installation(profile_id, installation_id)
    logger.info(
        "GitHub App %s for profile %s — installation %s",
        setup_action, profile_id, installation_id,
    )
    return RedirectResponse(url="/?github=connected")


async def _upsert_installation(profile_id: str, installation_id: str):
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(LinkedIdentity).where(
                    LinkedIdentity.profile_id == profile_id,
                    LinkedIdentity.provider == "github",
                )
            )
        ).scalar_one_or_none()

        if row:
            row.tenant_id = installation_id
        else:
            db.add(LinkedIdentity(
                profile_id=profile_id,
                provider="github",
                tenant_id=installation_id,
                workspace_label=settings.GITHUB_ORG,
            ))
        await db.commit()


async def _remove_installation(profile_id: str, installation_id: str):
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(LinkedIdentity).where(
                LinkedIdentity.profile_id == profile_id,
                LinkedIdentity.provider == "github",
                LinkedIdentity.tenant_id == installation_id,
            )
        )
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == "github",
                )
            )
        ).scalar_one_or_none()
        if row:
            row.sync_status = "error"
        await db.commit()
