"""
GitLab webhook receiver.
Verified via X-Gitlab-Token header (shared secret).
Push events create one activity entry per commit.
"""
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select

from app.auth.oauth import get_valid_token
from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.models import Integration, LinkedIdentity
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest, normalize
from app.webhooks.registration import _webhook_base

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
    object_kind = body.get("object_kind", "")
    namespace = (
        body.get("project", {}).get("path_with_namespace")
        or body.get("user_username")
    )
    profile_id = await _resolve_profile(namespace)
    if not profile_id:
        logger.warning("GitLab: no profile found for namespace=%r", namespace)
        return

    if object_kind == "push":
        # One event per commit
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


@router.post("/api/disconnect/gitlab")
async def disconnect_gitlab(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if isinstance(profile_id, bytes):
        profile_id = profile_id.decode()

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


@router.get("/api/gitlab/reregister")
async def reregister_gitlab_webhooks(request: Request):
    """Re-register GitLab webhooks, skipping projects that already have our URL."""
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if isinstance(profile_id, bytes):
        profile_id = profile_id.decode()

    token = await get_valid_token(profile_id, "gitlab")
    if not token:
        return JSONResponse({"error": "no_gitlab_token — connect GitLab first"}, status_code=400)

    headers    = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    target_url = f"{_webhook_base()}/webhook/gitlab"

    async with httpx.AsyncClient() as client:
        me = await client.get("https://gitlab.com/api/v4/user", headers=headers)
        if me.status_code != 200:
            return JSONResponse({"error": f"GitLab token invalid: {me.status_code}"}, status_code=400)
        username = me.json().get("username", "")

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

            # Check if our webhook URL is already registered — avoid duplicates
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

            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(LinkedIdentity).where(
                        LinkedIdentity.profile_id == profile_id,
                        LinkedIdentity.provider == "gitlab",
                        LinkedIdentity.workspace_label == namespace,
                    )
                )).scalar_one_or_none()
                if not row:
                    db.add(LinkedIdentity(
                        profile_id=profile_id,
                        provider="gitlab",
                        workspace_label=namespace,
                        tenant_id=username,
                    ))
                    await db.commit()

    registered = sum(1 for r in results if r["ok"])
    return JSONResponse({"registered": registered, "total": len(results), "projects": results})
