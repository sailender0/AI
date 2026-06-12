"""
Jira webhook receiver.
Verified via a shared secret passed as a query parameter.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import settings
from app.storage.models import LinkedIdentity
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest, normalize

router = APIRouter()
logger = logging.getLogger(__name__)


async def _resolve_profile(account_id: str | None) -> str | None:
    async with AsyncSessionLocal() as db:
        if account_id:
            row = (
                await db.execute(
                    select(LinkedIdentity).where(
                        LinkedIdentity.provider == "jira",
                        LinkedIdentity.tenant_id == account_id,
                    )
                )
            ).scalar_one_or_none()
            if row:
                return str(row.profile_id)

        # Fallback: return first profile with an active Jira integration
        from app.storage.models import Integration
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.source == "jira",
                    Integration.sync_status == "active",
                )
            )
        ).scalar_one_or_none()
        return str(row.profile_id) if row else None


async def _process(body: dict, jira_event: str):
    account_id = (body.get("user") or {}).get("accountId")
    profile_id = await _resolve_profile(account_id)
    if not profile_id:
        return
    event = normalize(body, source="jira", profile_id=profile_id, event_type=jira_event)
    await ingest(event)


@router.post("/webhook/jira")
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    # Jira passes the secret as a query param or Authorization header
    auth = request.headers.get("Authorization", "")
    qs_secret = request.query_params.get("secret", "")
    if qs_secret != settings.JIRA_WEBHOOK_SECRET and auth != f"Bearer {settings.JIRA_WEBHOOK_SECRET}":
        return JSONResponse({"error": "invalid_secret"}, status_code=401)

    body = await request.json()
    jira_event = body.get("webhookEvent", "")
    background_tasks.add_task(_process, body, jira_event)
    return JSONResponse({"status": "accepted"}, status_code=200)
