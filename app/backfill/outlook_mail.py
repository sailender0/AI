"""Outlook mail -> activity events. Correspondent address and timestamp ONLY.

Delegated `Mail.ReadBasic`, which structurally cannot return message bodies —
unlike chat, where Graph sends the body whether you ask or not. Subjects ARE
available here and are deliberately not stored: the spec is addresses and times.

Direction is decided here rather than in the normalizer, which has no access to
the profile's own address. That is why this is a fresh connector instead of a fix
to the me/messages webhook path — see the note in services/calendar_activity.py.
"""
import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import select

from app.auth.sso import acquire_delegated_token
from app.backfill import GRAPH, make_event, parse_iso, person_key, walk
from app.storage.models import Profile
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest

logger = logging.getLogger(__name__)

# Everything needed to say "who, and when". No subject, no bodyPreview.
_FIELDS = "id,from,toRecipients,receivedDateTime,sentDateTime"


def day_params(day: str) -> dict:
    """Query one day of mail.

    Filtered on receivedDateTime because it is set on every message in the
    mailbox, including the copies in Sent Items — one query covers both
    directions. The mail API supports $select properly, unlike chat.
    """
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return {
        "$top": 50,
        "$select": _FIELDS,
        "$orderby": "receivedDateTime desc",
        "$filter": (f"receivedDateTime ge {day}T00:00:00Z and "
                    f"receivedDateTime lt {nxt}T00:00:00Z"),
    }


def _addr(entry: dict) -> tuple[str, str]:
    """(address, display name) out of a Graph emailAddress wrapper."""
    e = (entry or {}).get("emailAddress") or {}
    return (e.get("address") or "").strip(), (e.get("name") or "").strip()


def mail_event(profile_id: str, msg: dict, self_email: str) -> dict | None:
    """One Graph message -> event dict, or None if there's no one to file it under.

    The row is filed under the *correspondent*, never under you: mail you sent
    belongs to its recipient, or filtering to a colleague would hide half the
    thread — the same rule the chat connector follows.
    """
    from_addr, from_name = _addr(msg.get("from"))
    sent_by_me = bool(from_addr) and from_addr.lower() == (self_email or "").lower()

    if sent_by_me:
        recipients = msg.get("toRecipients") or []
        if not recipients:
            return None  # a draft, or a message with only bcc — nothing to show
        addr, name = _addr(recipients[0])
        event_type, extra = "mail_sent", max(len(recipients) - 1, 0)
        occurred = msg.get("sentDateTime") or msg.get("receivedDateTime")
    else:
        addr, name = from_addr, from_name
        event_type, extra = "mail_received", 0
        occurred = msg.get("receivedDateTime") or msg.get("sentDateTime")

    if not addr:
        return None

    key = person_key(addr, addr)
    return make_event(
        profile_id=profile_id,
        source="outlook_mail",
        event_type=event_type,
        source_event_id=str(msg["id"]),
        title=addr,                      # the address IS the row — no subject line
        occurred_at=parse_iso(occurred),
        workspace=name or None,          # display name, when Graph supplies one
        raw={"user_id": key, "people": [key], "extra_recipients": extra},
    )


async def fetch_mail_events(client, token: str, profile_id: str, self_email: str,
                            day: str) -> list[dict]:
    """Every message sent or received on `day`, as event dicts."""
    msgs = await walk(client, f"{GRAPH}/me/messages",
                      {"Authorization": f"Bearer {token}"}, day_params(day))
    events = []
    for msg in msgs:
        if not msg.get("id"):
            continue
        event = mail_event(profile_id, msg, self_email)
        if event:
            events.append(event)
    return events


async def backfill_mail_day(profile_id: str, day: str) -> int:
    """Ingest one day of mail for one profile. Returns rows newly inserted.

    Returns 0 harmlessly when Mail.ReadBasic isn't consented: Graph answers 403
    and _walk yields nothing.
    """
    async with AsyncSessionLocal() as db:
        profile = (await db.execute(
            select(Profile).where(Profile.id == profile_id)
        )).scalar_one_or_none()

    # Without your own address every message looks incoming, so don't guess.
    if not profile or not profile.email:
        return 0

    token = await acquire_delegated_token(profile_id)
    if not token:
        return 0

    async with httpx.AsyncClient(timeout=30) as client:
        events = await fetch_mail_events(client, token, profile_id, profile.email, day)
    return sum([await ingest(event) for event in events])
