"""
Proactive Teams delivery via M365 Agents SDK / Bot Framework.
Summary text is wrapped in an Adaptive Card (structured JSON) — never raw HTML —
so the content cannot escape the card container.
"""
import logging
from datetime import datetime, timezone

import httpx
import msal
from sqlalchemy import select

from app.config import settings
from app.storage.models import Summary
from app.storage.postgres import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _build_adaptive_card(summary_text: str) -> dict:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {
                "type": "TextBlock",
                "text": "Your Activity Summary",
                "weight": "Bolder",
                "size": "Medium",
            },
            {
                "type": "TextBlock",
                "text": summary_text,
                "wrap": True,
            },
        ],
    }


async def _get_bot_token() -> str:
    app = msal.ConfidentialClientApplication(
        settings.AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}",
        client_credential=settings.AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://api.botframework.com/.default"])
    return result.get("access_token", "")


async def deliver_to_teams(profile, summary_text: str):
    if not profile.teams_user_id:
        logger.warning("No teams_user_id for profile %s — skipping delivery", profile.id)
        return

    token = await _get_bot_token()
    card = _build_adaptive_card(summary_text)

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }

    # Proactive message via Bot Framework direct-to-user endpoint
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://smba.trafficmanager.net/amer/v3/conversations/{profile.teams_user_id}/activities",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code not in (200, 201):
        logger.error("Teams delivery failed for %s: %s %s", profile.id, resp.status_code, resp.text)
        return

    async with AsyncSessionLocal() as db:
        summaries = (
            await db.execute(
                select(Summary).where(
                    Summary.profile_id == str(profile.id),
                    Summary.delivered_at.is_(None),
                )
            )
        ).scalars().all()
        for s in summaries:
            s.delivered_at = datetime.now(timezone.utc)
        await db.commit()
