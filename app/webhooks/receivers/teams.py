"""
Teams webhook receivers — change notifications and lifecycle events.

FIX (issue #2): backfill_teams_delta is implemented here.
Bot-message filter prevents the app from ingesting its own summary posts.
Must respond within 3 seconds; heavy work is dispatched as background tasks.
"""
import asyncio
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.auth.sso import acquire_delegated_token
from app.config import settings
from app.storage.models import Integration
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest, normalize
from app.webhooks.registration import auto_register_teams_subscription

router = APIRouter()
logger = logging.getLogger(__name__)


async def _fetch_message(resource: str, token: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://graph.microsoft.com/v1.0/{resource}",
            headers={"Authorization": f"Bearer {token}"},
        )
    return resp.json() if resp.status_code == 200 else None


async def _process_notification(notification: dict):
    profile_id = notification.get("clientState", "")
    resource = notification.get("resource", "")
    if not profile_id or not resource:
        return

    token = await acquire_delegated_token(profile_id)
    if not token:
        return

    message = await _fetch_message(resource, token)
    if not message:
        return

    sender_id = (message.get("from") or {}).get("user", {}).get("id", "")
    if sender_id == settings.BOT_SERVICE_PRINCIPAL_ID:
        return

    event = normalize(message, source="teams", profile_id=profile_id)
    await ingest(event)


@router.post("/webhook/teams")
async def teams_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    # Validation handshake — Microsoft sends validationToken on first registration
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return JSONResponse(content=validation_token, media_type="text/plain")

    for notification in body.get("value", []):
        background_tasks.add_task(_process_notification, notification)

    return JSONResponse(content={"status": "accepted"}, status_code=200)


@router.post("/webhook/teams/lifecycle")
async def teams_lifecycle(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return JSONResponse(content=validation_token, media_type="text/plain")

    for notification in body.get("value", []):
        profile_id = notification.get("clientState", "")
        event_type = notification.get("lifecycleEvent", "")

        if event_type == "subscriptionRemoved":
            background_tasks.add_task(auto_register_teams_subscription, profile_id)

        elif event_type == "missed":
            background_tasks.add_task(backfill_teams_delta, profile_id)

    return JSONResponse(content={"status": "accepted"}, status_code=202)


async def backfill_teams_delta(profile_id: str):
    """
    FIX (issue #2): Recover messages missed during a subscription gap
    using the Graph delta query for chats.
    Delta link is cached in Redis per profile; on first call a full sync runs.
    """
    from app.storage.redis_client import get_redis

    redis = get_redis()
    delta_key = f"teams_delta_link:{profile_id}"
    delta_link = await redis.get(delta_key)

    token = await acquire_delegated_token(profile_id)
    if not token:
        return

    url = delta_link or "https://graph.microsoft.com/v1.0/me/messages/delta"

    async with httpx.AsyncClient() as client:
        while url:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                break
            data = resp.json()

            for message in data.get("value", []):
                sender_id = (message.get("from") or {}).get("user", {}).get("id", "")
                if sender_id == settings.BOT_SERVICE_PRINCIPAL_ID:
                    continue
                event = normalize(message, source="teams", profile_id=profile_id)
                await ingest(event)

            next_link = data.get("@odata.nextLink")
            new_delta = data.get("@odata.deltaLink")

            if new_delta:
                await redis.set(delta_key, new_delta, ex=86400 * 7)
                break

            url = next_link
