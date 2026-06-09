"""
GitLab webhook receiver.
Verified via X-Gitlab-Token header (shared secret).
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import settings
from app.storage.models import LinkedIdentity
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest, normalize

router = APIRouter()
logger = logging.getLogger(__name__)


async def _resolve_profile(namespace: str | None) -> str | None:
    if not namespace:
        return None
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(LinkedIdentity).where(
                    LinkedIdentity.provider == "gitlab",
                    LinkedIdentity.workspace_label == namespace,
                )
            )
        ).scalar_one_or_none()
    return str(row.profile_id) if row else None


async def _process(body: dict):
    namespace = (
        body.get("project", {}).get("path_with_namespace")
        or body.get("user_username")
    )
    profile_id = await _resolve_profile(namespace)
    if not profile_id:
        return
    event = normalize(body, source="gitlab", profile_id=profile_id)
    await ingest(event)


@router.post("/webhook/gitlab")
async def gitlab_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str = Header(default=""),
):
    if x_gitlab_token != settings.GITLAB_WEBHOOK_SECRET:
        return JSONResponse({"error": "invalid_token"}, status_code=401)

    body = await request.json()
    background_tasks.add_task(_process, body)
    return JSONResponse({"status": "accepted"}, status_code=200)
