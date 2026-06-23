"""
Webhook / subscription registration — fires automatically after auth.

FIX (issue #3): Teams subscriptions use a delegated token from the MSAL cache
so that me/messages is a valid resource (it requires delegated Chat.Read scope).
"""
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.auth.oauth import get_valid_token
from app.auth.sso import acquire_delegated_token
from app.config import settings
from app.storage.models import Integration
from app.storage.postgres import AsyncSessionLocal


def _webhook_base() -> str:
    """Public URL used when registering webhooks with external services."""
    return settings.WEBHOOK_BASE_URL or settings.APP_BASE_URL


async def auto_register_teams_subscription(profile_id: str):
    """Subscribe to me/messages using the user's delegated token."""
    token = await acquire_delegated_token(profile_id)
    if not token:
        return

    payload = {
        "changeType": "created,updated",
        "notificationUrl": f"{_webhook_base()}/webhook/teams",
        "lifecycleNotificationUrl": f"{_webhook_base()}/webhook/teams/lifecycle",
        "resource": "me/messages",
        "expirationDateTime": (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat(),
        "clientState": profile_id,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code not in (200, 201):
        return

    data = resp.json()
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == "teams_subscription",
                )
            )
        ).scalar_one_or_none()

        if not row:
            row = Integration(profile_id=profile_id, source="teams_subscription")
            db.add(row)

        row.subscription_id = data["id"]
        row.subscription_expires_at = datetime.fromisoformat(data["expirationDateTime"].replace("Z", "+00:00"))
        row.sync_status = "active"
        await db.commit()


async def auto_register_webhook(source: str, profile_id: str):
    token = await get_valid_token(profile_id, source)
    if not token:
        return

    if source == "github":
        await _register_github(token, profile_id)
    elif source == "gitlab":
        await _register_gitlab(token, profile_id)
    elif source == "jira":
        await _register_jira(token, profile_id)


async def _register_github(token: str, profile_id: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/orgs/{settings.GITHUB_ORG}/hooks",
            json={
                "config": {
                    "url": f"{_webhook_base()}/webhook/github",
                    "content_type": "json",
                    "secret": settings.GITHUB_WEBHOOK_SECRET,
                },
                "events": ["push", "pull_request", "pull_request_review", "issues"],
                "active": True,
            },
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )

    if resp.status_code not in (200, 201):
        return

    hook_id = str(resp.json().get("id", ""))
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == "github",
                )
            )
        ).scalar_one_or_none()
        if row:
            row.github_hook_id = hook_id
            await db.commit()


async def _register_gitlab(token: str, profile_id: str):
    import logging as _log
    logger = _log.getLogger(__name__)

    headers    = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    target_url = f"{_webhook_base()}/webhook/gitlab"
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

    async with httpx.AsyncClient() as client:
        me_resp = await client.get("https://gitlab.com/api/v4/user", headers=headers)
        username = me_resp.json().get("username", "") if me_resp.status_code == 200 else ""

        projects_resp = await client.get(
            "https://gitlab.com/api/v4/projects",
            params={"membership": True, "per_page": 100, "simple": True},
            headers=headers,
        )

    if projects_resp.status_code != 200:
        logger.error("GitLab projects fetch failed: %s %s", projects_resp.status_code, projects_resp.text)
        return

    projects = projects_resp.json()
    if not projects:
        logger.warning("GitLab: no projects found for profile %s", profile_id)
        return

    from app.storage.models import LinkedIdentity

    registered = 0
    async with httpx.AsyncClient() as client:
        for project in projects:
            project_id = project["id"]
            namespace  = project.get("path_with_namespace", "")

            # Skip if our URL is already registered — avoid duplicate webhook deliveries
            existing_hooks_resp = await client.get(
                f"https://gitlab.com/api/v4/projects/{project_id}/hooks",
                headers=headers,
            )
            if existing_hooks_resp.status_code == 200:
                if any(h.get("url") == target_url for h in existing_hooks_resp.json()):
                    logger.info("GitLab webhook already exists for %s, skipping", namespace)
                    registered += 1
                    # still ensure LinkedIdentity exists (fall through to upsert below)
                else:
                    resp = await client.post(
                        f"https://gitlab.com/api/v4/projects/{project_id}/hooks",
                        json=hook_payload,
                        headers=headers,
                    )
                    if resp.status_code in (200, 201):
                        registered += 1
                        logger.info("GitLab webhook registered for %s (profile=%s)", namespace, profile_id)
                    else:
                        logger.warning("GitLab webhook failed for %s: %s %s", namespace, resp.status_code, resp.text)

            async with AsyncSessionLocal() as db:
                existing = (await db.execute(
                    select(LinkedIdentity).where(
                        LinkedIdentity.profile_id == profile_id,
                        LinkedIdentity.provider == "gitlab",
                        LinkedIdentity.workspace_label == namespace,
                    )
                )).scalar_one_or_none()
                if not existing:
                    db.add(LinkedIdentity(
                        profile_id=profile_id,
                        provider="gitlab",
                        workspace_label=namespace,
                        tenant_id=username,
                    ))
                    await db.commit()

    logger.info("GitLab: %d/%d webhooks active for profile %s", registered, len(projects), profile_id)

    if registered == 0:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == "gitlab",
                )
            )).scalar_one_or_none()
            if row:
                row.sync_status = "error"
                await db.commit()
        logger.error("GitLab: no webhooks registered for profile %s — marked error", profile_id)


async def _register_jira(token: str, profile_id: str):
    async with httpx.AsyncClient() as client:
        # Jira OAuth 2.0 tokens require api.atlassian.com with a cloud ID
        resources_resp = await client.get(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if resources_resp.status_code != 200 or not resources_resp.json():
            return
        cloud_id = resources_resp.json()[0]["id"]

        webhook_url = f"{_webhook_base()}/webhook/jira?secret={settings.JIRA_WEBHOOK_SECRET}"
        resp = await client.post(
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/webhook",
            json={
                "url": webhook_url,
                "webhooks": [
                    {
                        "events": ["jira:issue_created", "jira:issue_updated", "comment_created"],
                        "jqlFilter": "project != \"\"",
                    }
                ],
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    import logging as _logging
    _log = _logging.getLogger(__name__)
    _log.info("Jira webhook registration: %s %s", resp.status_code, resp.text)

    if resp.status_code not in (200, 201):
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == "jira",
                )
            )).scalar_one_or_none()
            if row:
                row.sync_status = "error"
                await db.commit()
        _log.error("Jira webhook registration failed for profile %s: %s", profile_id, resp.text)
        return

    webhook_ids = resp.json().get("webhookRegistrationResult", [])
    hook_id = str(webhook_ids[0].get("createdWebhookId", "")) if webhook_ids else str(resp.json().get("id", ""))
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.source == "jira",
                )
            )
        ).scalar_one_or_none()
        if row:
            row.jira_webhook_id = hook_id
            row.jira_webhook_expires_at = expires_at
            await db.commit()
