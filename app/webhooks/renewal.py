"""Background renewal jobs — run via APScheduler."""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.auth.oauth import get_valid_token
from app.auth.sso import acquire_delegated_token
from app.config import settings
from app.storage.models import GitHubIntegration, Integration, JiraIntegration, TeamsIntegration
# AsyncSessionLocal() is used intentionally here — renewal jobs are APScheduler
# background tasks, not FastAPI requests, so Depends(get_db) is unavailable.
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.registration import auto_register_teams_subscription, auto_register_webhook

logger = logging.getLogger(__name__)


async def renew_teams_subscriptions():
    """Renew Teams Graph subscriptions expiring within 15 minutes. Runs every 45 minutes."""
    threshold = datetime.now(timezone.utc) + timedelta(minutes=15)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(TeamsIntegration).where(
                    TeamsIntegration.subscription_expires_at < threshold,
                    TeamsIntegration.sync_status == "active",
                )
            )
        ).scalars().all()

    logger.info("Teams renewal: %d subscription(s) expiring within 15 min", len(rows))
    for row in rows:
        profile_id = str(row.profile_id)
        token = await acquire_delegated_token(profile_id)
        if not token:
            continue
        try:
            new_expiry = (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()
            # First session closes before the HTTP call so no connection is held
            # during network I/O. A second session handles the write after.
            async with httpx.AsyncClient() as client:
                resp = await client.patch(
                    f"https://graph.microsoft.com/v1.0/subscriptions/{row.subscription_id}",
                    json={"expirationDateTime": new_expiry},
                    headers={"Authorization": f"Bearer {token}"},
                )

            if resp.status_code == 404:
                await auto_register_teams_subscription(profile_id)
            elif resp.status_code == 200:
                async with AsyncSessionLocal() as db:
                    r = await db.get(Integration, row.id)
                    if r:
                        r.subscription_expires_at = datetime.now(timezone.utc) + timedelta(minutes=55)
                        await db.commit()
        except Exception as exc:
            logger.error("Teams renewal failed for %s: %s", profile_id, exc)


async def renew_jira_webhooks():
    """Renew Jira webhooks expiring within 12 days. Runs every 20 days."""
    threshold = datetime.now(timezone.utc) + timedelta(days=12)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(JiraIntegration).where(
                    JiraIntegration.jira_webhook_expires_at < threshold,
                    JiraIntegration.sync_status == "active",
                )
            )
        ).scalars().all()

    logger.info("Jira renewal: %d webhook(s) expiring within 12 days", len(rows))
    if not rows:
        return

    for row in rows:
        profile_id = str(row.profile_id)
        token = await get_valid_token(profile_id, "jira")
        if not token:
            continue
        try:
            # OAuth tokens only work via api.atlassian.com with a cloud id —
            # same as registration/backfill; a site-URL call would just 401.
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    "https://api.atlassian.com/oauth/token/accessible-resources", headers=headers)
                cloud_id = res.json()[0]["id"] if res.status_code == 200 and res.json() else None
                resp = None
                if cloud_id:
                    resp = await client.put(
                        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/webhook/refresh",
                        json={"webhookIds": [int(row.jira_webhook_id)]},
                        headers=headers,
                    )

            if resp is not None and resp.status_code == 200:
                async with AsyncSessionLocal() as db:
                    r = await db.get(Integration, row.id)
                    if r:
                        r.jira_webhook_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
                        await db.commit()
            else:
                await auto_register_webhook("jira", profile_id)
        except Exception as exc:
            logger.error("Jira renewal failed for %s: %s", profile_id, exc)


async def check_github_webhook_health():
    """Detect silently disabled GitHub webhooks. Runs every 6 hours."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(GitHubIntegration).where(
                    GitHubIntegration.sync_status == "active",
                )
            )
        ).scalars().all()

    logger.info("GitHub health check: %d active integration(s) to verify", len(rows))
    for row in rows:
        profile_id = str(row.profile_id)
        token = await get_valid_token(profile_id, "github")
        if not token or not row.github_hook_id:
            continue
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.github.com/orgs/{settings.GITHUB_ORG}/hooks/{row.github_hook_id}",
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )

            hook = resp.json() if resp.status_code == 200 else None
            if hook is None or not hook.get("active"):
                logger.warning("GitHub webhook disabled for profile %s", profile_id)
                await auto_register_webhook("github", profile_id)
        except Exception as exc:
            logger.error("GitHub health check failed for %s: %s", profile_id, exc)
