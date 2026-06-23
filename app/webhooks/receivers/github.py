"""
GitHub webhook receiver.
Signature verified via HMAC-SHA256 (X-Hub-Signature-256).
Must respond within 10 seconds; processing runs in background.
"""
import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import settings
from app.middleware.rate_limit import limiter
from app.storage.models import LinkedIdentity
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest, normalize

router = APIRouter()
logger = logging.getLogger(__name__)


def _verify_signature(body: bytes, signature: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.GITHUB_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature[7:])


async def _resolve_profile(installation_id: str | None) -> str | None:
    if not installation_id:
        return None
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(LinkedIdentity).where(
                    LinkedIdentity.provider == "github",
                    LinkedIdentity.tenant_id == str(installation_id),
                )
            )
        ).scalar_one_or_none()
    return str(row.profile_id) if row else None


_GITHUB_TYPE_MAP = {
    "push": "commit",
    "issues": "issue_updated",
    "pull_request_review": "pr_review",
    "issue_comment": "comment",
    # pull_request → left as None so normalizer derives it from action
}


async def _process(body: dict, event_type: str):
    installation = body.get("installation", {})
    installation_id = str(installation.get("id", "")) if installation else None
    profile_id = await _resolve_profile(installation_id)
    logger.info("GitHub event=%s installation_id=%s profile_id=%s", event_type, installation_id, profile_id)
    if not profile_id:
        return
    mapped = _GITHUB_TYPE_MAP.get(event_type)          # None = let normalizer decide
    if mapped is None and event_type != "pull_request":
        mapped = event_type                             # unknown types pass through as-is
    event = normalize(body, source="github", profile_id=profile_id, event_type=mapped)
    await ingest(event)


@router.post("/webhook/github")
@limiter.limit("200/minute")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
):
    body_bytes = await request.body()
    if not _verify_signature(body_bytes, x_hub_signature_256):
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    body = await request.json()
    background_tasks.add_task(_process, body, x_github_event)
    return JSONResponse({"status": "accepted"}, status_code=200)
