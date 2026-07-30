"""Teams chat -> activity events. Counterparty name and timestamp ONLY.

Delegated `Chat.Read`. Three Graph facts drive the shape of this module:

* There is no delegated all-chats endpoint — `getAllMessages` is application-only
  (the Teams export API). So this is N+1: list `/me/chats`, then walk
  `/chats/{id}/messages` per chat.
* `/chats/{id}/messages` does NOT support `$select`, so `body.content` arrives on
  every message whether we want it or not. Dropping it in the mapper is the only
  control there is — `make_event` is never passed `raw` verbatim, and `title`
  holds a person's name, never message text.
* `$filter` is SILENTLY IGNORED unless `$orderby` names the same property, and
  `createdDateTime` only supports `lt`. See day_params().

Do NOT route chat through webhooks.normalizer.normalize(), which stores the whole
Graph object in raw_payload.
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

# members comes back with the chat list, so resolving a 1:1 counterparty costs no
# extra request. Graph caps the expansion at 25 members regardless of $top —
# irrelevant for oneOnOne, and group chats don't use it (see chat_counterparty).
_CHATS_PARAMS = {"$top": 50, "$expand": "members"}


def day_params(day: str) -> dict:
    """Query one day (YYYY-MM-DD) of messages in a chat.

    The range filter has to be on lastModifiedDateTime: createdDateTime supports
    only `lt`, and Graph ignores $filter outright — 200 OK, every message, no
    error — unless $orderby names the same property. Callers re-check
    createdDateTime when bucketing, since a message written last month and edited
    today lands inside today's window.
    """
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return {
        "$top": 50,  # documented maximum
        "$orderby": "lastModifiedDateTime desc",
        "$filter": (f"lastModifiedDateTime gt {day}T00:00:00.000Z and "
                    f"lastModifiedDateTime lt {nxt}T00:00:00.000Z"),
    }


def _members(chat: dict) -> dict:
    """userId -> member, for resolving a sender's address from the chat roster."""
    return {m["userId"]: m for m in (chat.get("members") or []) if m.get("userId")}


def chat_counterparty(chat: dict, msg: dict, self_id: str) -> dict | None:
    """Who this message is *with* — the person the calendar row is filed under.

    Someone else sent it  -> them, in any chat type.
    You sent it, 1:1      -> the other member.
    You sent it, group    -> None. A group post has no counterparty, and inventing
                             one would put the row under whoever happened to be
                             listed first.

    ponytail: dropping your own group posts loses "you were active in the Design
    Guild chat at 14:00". Add a null-counterparty row type if that turns out to
    matter — the data is all here.
    """
    members = _members(chat)
    sender = (msg.get("from") or {}).get("user") or {}

    if sender.get("id") and sender["id"] != self_id:
        roster = members.get(sender["id"], {})
        return {
            "id": person_key(roster.get("email"), sender["id"]),
            "name": sender.get("displayName") or roster.get("displayName") or "",
            "from_self": False,
        }

    if chat.get("chatType") != "oneOnOne":
        return None
    for uid, member in members.items():
        if uid != self_id:
            return {"id": person_key(member.get("email"), uid),
                    "name": member.get("displayName") or "", "from_self": True}
    return None


def chat_message_event(profile_id: str, chat: dict, msg: dict, self_id: str) -> dict | None:
    """One Graph chatMessage -> event dict, or None if it isn't user activity.

    Keeps the counterparty's name and the creation time. Body, attachments,
    mentions and reactions are dropped here and never stored.
    """
    if msg.get("messageType") != "message" or msg.get("isDeleted"):
        return None  # systemEventMessage: "X added Y", "call started", chat renamed
    if not ((msg.get("from") or {}).get("user")):
        return None  # bot/app message: from.user is null, from.application is set

    other = chat_counterparty(chat, msg, self_id)
    if not other or not other["name"]:
        return None

    return make_event(
        profile_id=profile_id,
        source="teams_chat",
        event_type="chat_message",
        source_event_id=str(msg["id"]),  # message id -> re-polls dedup via the unique index
        title=other["name"],
        occurred_at=parse_iso(msg.get("createdDateTime")),
        workspace=chat.get("chatType"),  # oneOnOne | group | meeting
        # Deliberately built, NOT the Graph object. `people` is what the person
        # filter queries — one array on every event type, so a meeting matches on
        # any attendee while a chat matches its single counterparty. `user_id` is
        # who the row displays as. Names collide and get renamed; ids don't.
        raw={"user_id": other["id"], "people": [other["id"]],
             "from_self": other["from_self"], "chat_type": chat.get("chatType", "")},
    )


async def fetch_chat_events(client, token: str, profile_id: str, self_id: str,
                            day: str) -> list[dict]:
    """Every chat message the user sent or received on `day`, as event dicts."""
    headers = {"Authorization": f"Bearer {token}"}
    chats = await walk(client, f"{GRAPH}/me/chats", headers, _CHATS_PARAMS)

    events = []
    for chat in chats:
        if not chat.get("id"):
            continue
        msgs = await walk(client, f"{GRAPH}/chats/{chat['id']}/messages",
                          headers, day_params(day))
        for msg in msgs:
            # Filtered on lastModifiedDateTime, so bucket on createdDateTime.
            if not str(msg.get("createdDateTime", "")).startswith(day):
                continue
            event = chat_message_event(profile_id, chat, msg, self_id)
            if event:
                events.append(event)
    return events


async def backfill_chat_day(profile_id: str, day: str) -> int:
    """Ingest one day of chat activity for one profile. Returns rows newly inserted.

    Returns 0 harmlessly when the scope isn't consented yet: the token comes back
    without Chat.Read, Graph answers 403, and _walk yields nothing.

    ponytail: N+1 Graph calls — schedule it, never call it from a page view.
    """
    async with AsyncSessionLocal() as db:
        profile = (await db.execute(
            select(Profile).where(Profile.id == profile_id)
        )).scalar_one_or_none()

    # teams_user_id is the Entra oid, set from the id_token claim at sign-in.
    # Without it we can't tell your messages from theirs, so don't guess.
    if not profile or not profile.teams_user_id:
        return 0

    token = await acquire_delegated_token(profile_id)
    if not token:
        return 0  # MSAL cache expired; the user re-grants at next sign-in

    async with httpx.AsyncClient(timeout=30) as client:
        events = await fetch_chat_events(client, token, profile_id,
                                         profile.teams_user_id, day)
    return sum([await ingest(event) for event in events])


