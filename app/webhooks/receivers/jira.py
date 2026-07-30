"""
Jira webhook receiver.
Verified via a shared secret passed as a query parameter (Jira's webhook API
does not support custom delivery headers, so query param is the only option).
"""
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import settings
from app.middleware.rate_limit import limiter
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

        # Fallback: only when EXACTLY ONE active Jira integration exists — an
        # unambiguous single-tenant deployment. With 2+ tenants, attributing an
        # unmatched event to a guessed profile cross-contaminates timelines (the
        # webhook secret is shared, so this path is spoofable) — drop it instead.
        from app.storage.models import Integration
        rows = (
            await db.execute(
                select(Integration).where(
                    Integration.source == "jira",
                    Integration.sync_status == "active",
                )
            )
        ).scalars().all()
        return str(rows[0].profile_id) if len(rows) == 1 else None


async def _process(body: dict, jira_event: str):
    account_id = (body.get("user") or {}).get("accountId")
    profile_id = await _resolve_profile(account_id)
    if not profile_id:
        return
    event = normalize(body, source="jira", profile_id=profile_id, event_type=jira_event)
    await ingest(event)


@router.post("/webhook/jira")
@limiter.limit("200/minute")
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    # Jira passes the secret as a query param or Authorization header
    auth = request.headers.get("Authorization", "")
    qs_secret = request.query_params.get("secret", "")
    secret = settings.JIRA_WEBHOOK_SECRET
    # Fail closed: without a configured secret, compare_digest("", "") is true and
    # every unauthenticated request would pass. No secret → reject.
    if not secret:
        return JSONResponse({"error": "invalid_secret"}, status_code=401)
    qs_ok = hmac.compare_digest(qs_secret, secret)
    auth_ok = hmac.compare_digest(auth, f"Bearer {secret}")
    if not qs_ok and not auth_ok:
        return JSONResponse({"error": "invalid_secret"}, status_code=401)

    body = await request.json()
    jira_event = body.get("webhookEvent", "")
    background_tasks.add_task(_process, body, jira_event)
    return JSONResponse({"status": "accepted"}, status_code=200)
