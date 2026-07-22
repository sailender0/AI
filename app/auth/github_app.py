"""
GitHub App installation callback.
GitHub redirects here after a user installs / updates / uninstalls the App.
No manual setup needed — installation_id is mapped to the logged-in profile automatically.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, delete, update

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
    # App install gives us org-wide webhooks but NOT the installer's identity.
    # Chain into OAuth to capture the GitHub user id (so webhooks resolve to this
    # profile by actor) and the backfill token. Without this the receiver's
    # actor-filter would drop this user's events.
    return RedirectResponse(url="/connect/github")


async def disconnect_installation(installation_id: str):
    """Uninstalling the App from GitHub's org settings page only fires the
    installation/deleted webhook — it never hits /github/app/callback. One
    installation is shared org-wide, so drop every profile's github link on it
    and flag their integrations 'disconnected' (NOT 'error' — get_integrations
    still counts 'error' as connected). This is what makes the UI show the
    'Install GitHub App' banner again."""
    if not installation_id:
        return
    async with AsyncSessionLocal() as db:
        profile_ids = (await db.execute(
            select(LinkedIdentity.profile_id).where(
                LinkedIdentity.provider == "github",
                LinkedIdentity.tenant_id == str(installation_id),
            )
        )).scalars().all()
        if not profile_ids:
            return
        await db.execute(
            delete(LinkedIdentity).where(
                LinkedIdentity.provider == "github",
                LinkedIdentity.tenant_id == str(installation_id),
            )
        )
        await db.execute(
            update(Integration)
            .where(Integration.profile_id.in_(profile_ids), Integration.source == "github")
            .values(sync_status="disconnected")
        )
        await db.commit()
        logger.info(
            "GitHub App uninstalled (installation %s) — disconnected %d profile(s)",
            installation_id, len(profile_ids),
        )


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


async def _remove_installation(profile_id: str, _installation_id: str):
    async with AsyncSessionLocal() as db:
        # Remove every github link for this profile (both the install row and the
        # OAuth identity row keyed on the user id), not just the installation_id.
        await db.execute(
            delete(LinkedIdentity).where(
                LinkedIdentity.profile_id == profile_id,
                LinkedIdentity.provider == "github",
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
            row.sync_status = "disconnected"
        await db.commit()
