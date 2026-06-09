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


async def auto_register_teams_subscription(profile_id: str):
    """Subscribe to me/messages using the user's delegated token."""
    token = await acquire_delegated_token(profile_id)
    if not token:
        return

    payload = {
        "changeType": "created,updated",
        "notificationUrl": f"{settings.APP_BASE_URL}/webhook/teams",
        "lifecycleNotificationUrl": f"{settings.APP_BASE_URL}/webhook/teams/lifecycle",
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
                    "url": f"{settings.APP_BASE_URL}/webhook/github",
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
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://gitlab.com/api/v4/projects/{settings.GITLAB_PROJECT_ID}/hooks",
            json={
                "url": f"{settings.APP_BASE_URL}/webhook/gitlab",
                "token": settings.GITLAB_WEBHOOK_SECRET,
                "merge_requests_events": True,
                "push_events": True,
                "pipeline_events": True,
                "issues_events": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    # GitLab webhooks never expire — no expiry field to store


async def _register_jira(token: str, profile_id: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.JIRA_BASE_URL}/rest/api/3/webhook",
            json={
                "url": f"{settings.APP_BASE_URL}/webhook/jira",
                "webhookEvents": ["jira:issue_created", "jira:issue_updated", "comment_created"],
                "filters": {"issue-related-events-section": ""},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code not in (200, 201):
        return

    webhook_ids = resp.json().get("webhookRegistrationResult", [])
    hook_id = str(webhook_ids[0].get("createdWebhookId", "")) if webhook_ids else ""
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
