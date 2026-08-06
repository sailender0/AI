"""
GitLab webhook receiver.
Verified via X-Gitlab-Token header (shared secret).
Push events create one activity entry per commit.
"""
import hmac
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select

from app.auth.oauth import get_valid_token
from app.auth.sso import get_profile_from_session
from app.config import settings
from app.middleware.rate_limit import limiter
from app.storage.models import Integration, LinkedIdentity
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest, normalize
from app.webhooks.registration import _webhook_base, save_gitlab_identity

router = APIRouter()
logger = logging.getLogger(__name__)


async def _resolve_profile(actor_id: str | None) -> str | None:
    """Map a webhook's actor (the GitLab user who triggered it) to the profile
    that owns that identity. Resolving by actor — NOT by project — means a shared
    project attributes each event to whoever actually did it, and two people
    connecting the same project no longer collide. Mirrors the github receiver."""
    if not actor_id:
        return None
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(LinkedIdentity).where(
                    LinkedIdentity.provider == "gitlab",
                    LinkedIdentity.tenant_id == str(actor_id),
                )
            )
        ).scalar_one_or_none()
    return str(row.profile_id) if row else None


def _actor_id(body: dict) -> str | None:
    aid = body.get("user_id") or (body.get("user") or {}).get("id")
    return str(aid) if aid else None


async def _process(body: dict):
    object_kind = body.get("object_kind", "")
    actor_id = _actor_id(body)
    profile_id = await _resolve_profile(actor_id)
    if not profile_id:
        logger.warning("GitLab: no profile for actor_id=%r kind=%r", actor_id, object_kind)
        return

    if object_kind == "push":
        commits = body.get("commits") or []
        if not commits:
            return
        for commit in commits:
            enriched = dict(body)
            enriched["_commit"] = commit
            event = normalize(enriched, source="gitlab", profile_id=profile_id,
                              event_type="commit")
            await ingest(event)
    else:
        event = normalize(body, source="gitlab", profile_id=profile_id)
        await ingest(event)


@router.post("/webhook/gitlab")
@limiter.limit("200/minute")
async def gitlab_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str = Header(default=""),
):
    secret = settings.GITLAB_WEBHOOK_SECRET
    if not secret or not hmac.compare_digest(x_gitlab_token, secret):
        return JSONResponse({"error": "invalid_token"}, status_code=401)

    body = await request.json()
    background_tasks.add_task(_process, body)
    return JSONResponse({"status": "accepted"}, status_code=200)


@router.post("/api/disconnect/gitlab")
async def disconnect_gitlab(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        await db.execute(delete(Integration).where(
            Integration.profile_id == profile_id,
            Integration.source == "gitlab",
        ))
        await db.execute(delete(LinkedIdentity).where(
            LinkedIdentity.profile_id == profile_id,
            LinkedIdentity.provider == "gitlab",
        ))
        await db.commit()

    return JSONResponse({"ok": True})


@router.post("/api/gitlab/reregister")
async def reregister_gitlab_webhooks(request: Request):
    """Re-register GitLab webhooks, skipping projects that already have our URL."""
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    token = await get_valid_token(profile_id, "gitlab")
    if not token:
        return JSONResponse({"error": "no_gitlab_token — connect GitLab first"}, status_code=400)

    headers    = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    target_url = f"{_webhook_base()}/webhook/gitlab"

    async with httpx.AsyncClient() as client:
        me = await client.get("https://gitlab.com/api/v4/user", headers=headers)
        if me.status_code != 200:
            return JSONResponse({"error": f"GitLab token invalid: {me.status_code}"}, status_code=400)
        me_data  = me.json()
        username = me_data.get("username", "")
        user_id  = str(me_data.get("id", ""))

        projects_resp = await client.get(
            "https://gitlab.com/api/v4/projects",
            params={"membership": True, "per_page": 100, "simple": True},
            headers=headers,
        )

    if projects_resp.status_code != 200:
        return JSONResponse({"error": f"Failed to fetch projects: {projects_resp.status_code}"}, status_code=400)

    projects = projects_resp.json()
    if not projects:
        return JSONResponse({"error": "No GitLab projects found for this account"}, status_code=400)

    await save_gitlab_identity(profile_id, user_id, username)

    hook_payload = {
        "url": target_url,
        "token": settings.GITLAB_WEBHOOK_SECRET,
        "push_events": True,
        "merge_requests_events": True,
        "issues_events": True,
        "note_events": True,
        "pipeline_events": True,
        "tag_push_events": True,
    }

    results = []
    async with httpx.AsyncClient() as client:
        for project in projects:
            pid       = project["id"]
            namespace = project.get("path_with_namespace", "")

            existing_hooks = await client.get(
                f"https://gitlab.com/api/v4/projects/{pid}/hooks",
                headers=headers,
            )
            already_registered = False
            if existing_hooks.status_code == 200:
                already_registered = any(
                    h.get("url") == target_url
                    for h in existing_hooks.json()
                )

            if already_registered:
                results.append({"project": namespace, "status": 200, "ok": True, "detail": "already registered"})
                logger.info("GitLab webhook already exists for %s", namespace)
            else:
                resp = await client.post(
                    f"https://gitlab.com/api/v4/projects/{pid}/hooks",
                    json=hook_payload,
                    headers=headers,
                )
                success = resp.status_code in (200, 201)
                results.append({"project": namespace, "status": resp.status_code,
                                "ok": success, "detail": resp.text if not success else ""})
                logger.info("GitLab webhook %s for %s: %s", "OK" if success else "FAILED", namespace, resp.status_code)

    registered = sum(1 for r in results if r["ok"])
    return JSONResponse({"registered": registered, "total": len(results), "projects": results})
