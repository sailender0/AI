"""Send email as the signed-in user via Microsoft Graph (delegated Mail.Send).

Reuses the per-profile MSAL token cache (acquire_delegated_token), so the SAME
path serves on-demand sends and unattended scheduled digests. Recipient is always
the user themselves (self-only) — callers pass profile.email.

This is the one swappable interface: to move to app-only (send from a system
mailbox) later, only this function changes — get an app-only token and POST to
/users/{sender}/sendMail. Callers stay identical.
"""
import logging

import httpx

from app.auth.sso import acquire_delegated_token

logger = logging.getLogger(__name__)

_GRAPH_SENDMAIL = "https://graph.microsoft.com/v1.0/me/sendMail"


async def send_mail(profile_id: str, to: str, subject: str, html_body: str) -> bool:
    """Send an HTML email from the user's own mailbox to `to`. Returns True on 202.

    Subject is newline-stripped to block header injection; `html_body` must already
    be escaped by the renderer (see app/services/email_report.py).
    """
    token = await acquire_delegated_token(profile_id)
    if not token:
        logger.warning(
            "No Graph token for profile %s — user must sign in again to grant Mail.Send",
            profile_id,
        )
        return False

    payload = {
        "message": {
            "subject": subject.replace("\r", " ").replace("\n", " ")[:255],
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _GRAPH_SENDMAIL,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        logger.error("sendMail request failed for %s: %s", profile_id, exc)
        return False

    if resp.status_code == 202:
        return True
    logger.error("sendMail failed for %s: %s %s", profile_id, resp.status_code, resp.text[:300])
    return False
