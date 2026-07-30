"""Teams call records -> activity events. Counterparty name and duration ONLY.

`CallRecords.Read.All` is application-only — there is no delegated variant, so
this is the one connector that runs as the app rather than on a user's behalf.
That also makes it tenant-wide: one sweep collects the whole tenant's records for
a day, and each is attributed to whichever profiles were participants.

Two Graph facts shape this:

* The list endpoint filters on startDateTime but "the listed call records don't
  include expandable relationships such as sessions and participants_v2" — so
  every record needs a second GET with $expand=participants_v2 to learn who was
  on it. List-then-expand, not one call.
* Records live **30 days** and requests for older ones 404. There is no backfill:
  a polling gap is permanently lost, unlike mail, chat and calendar.

Records carrying a joinWebUrl are scheduled meetings, which the calendar
connector already covers — those are skipped here so a meeting doesn't appear
twice. See the note in call_event().
"""
import logging
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

import httpx
from sqlalchemy import select

from app.auth.sso import acquire_app_token
from app.backfill import GRAPH, make_event, parse_iso, person_key, walk
from app.storage.models import Profile
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest

logger = logging.getLogger(__name__)


def day_params(day: str, now: datetime | None = None) -> dict:
    """Records that started on `day` (UTC). Local-day bucketing happens at read
    time in services/calendar_activity.py, which converts to the viewer's zone.

    The window is clamped to `now`: Graph rejects the whole query with a 400 if
    the filter reaches into the future, so polling today with a naive midnight
    upper bound returns nothing at all. It also rejects anything older than 30
    days — the retention limit — which is why nothing here tries to backfill.
    """
    now = now or datetime.now(timezone.utc)
    end = datetime.combine(date.fromisoformat(day) + timedelta(days=1),
                           dtime.min, tzinfo=timezone.utc)
    end = min(end, now)
    return {"$filter": (f"startDateTime ge {day}T00:00:00Z and "
                        f"startDateTime lt {end.strftime('%Y-%m-%dT%H:%M:%SZ')}")}


def _identity(participant: dict) -> dict | None:
    """(oid, display name, upn) out of a participants_v2 entry, users only.

    Phone and ACS participants have no user identity — a PSTN caller carries a
    number instead, which is not a colleague and has no place in the roster.
    """
    user = ((participant or {}).get("identity") or {}).get("user") or {}
    if not user.get("id"):
        return None
    return {"oid": user["id"],
            "name": user.get("displayName") or "",
            "upn": (user.get("userPrincipalName") or "").strip()}


def call_event(profile_id: str, record: dict, participants: list[dict],
               self_oid: str) -> dict | None:
    """One call record -> an event for one participant's calendar.

    ponytail: records with a joinWebUrl are scheduled meetings and are skipped —
    the calendar connector already stores those, and a second row would double
    every meeting. Joining them on joinWebUrl to add "you joined 10:02-10:47" is
    the obvious next step, and it needs sessions($expand=segments): participants_v2
    carries identities but NOT per-person join and leave times.
    """
    if record.get("joinWebUrl"):
        return None

    others = [p for p in participants if p["oid"] != self_oid]
    if not others:
        return None  # a call with only you in it — a test call or a lone dial-in

    start, end = record.get("startDateTime"), record.get("endDateTime")
    started = parse_iso(start)
    minutes = max(int((parse_iso(end) - started).total_seconds() // 60), 0) if end else 0

    keys = [person_key(p["upn"], p["oid"]) for p in others]
    return make_event(
        profile_id=profile_id,
        source="teams_call",
        event_type="call",
        source_event_id=str(record["id"]),
        title=others[0]["name"] or others[0]["upn"] or "Unknown",
        occurred_at=started,
        workspace=record.get("type") or None,   # peerToPeer | groupCall
        raw={"user_id": keys[0], "people": keys, "minutes": minutes,
             "participants": [p["name"] for p in others if p["name"]]},
    )


async def fetch_call_events(client, token: str, profiles_by_oid: dict, day: str) -> list[dict]:
    """The whole tenant's ad-hoc calls for `day`, as events per participating profile."""
    headers = {"Authorization": f"Bearer {token}"}
    records = await walk(client, f"{GRAPH}/communications/callRecords",
                         headers, day_params(day))

    events = []
    for record in records:
        if not record.get("id") or record.get("joinWebUrl"):
            continue  # skip meetings before spending a request expanding them

        resp = await client.get(f"{GRAPH}/communications/callRecords/{record['id']}",
                                headers=headers, params={"$expand": "participants_v2"})
        if resp.status_code != 200:
            logger.warning("Graph %s expanding call record", resp.status_code)
            continue
        full = resp.json()

        participants = [p for p in (_identity(e) for e in full.get("participants_v2") or []) if p]
        for person in participants:
            profile_id = profiles_by_oid.get(person["oid"])
            if not profile_id:
                continue  # a colleague without an account here — not our row to write
            event = call_event(profile_id, full, participants, person["oid"])
            if event:
                events.append(event)
    return events


async def run_call_poll(day: str) -> int:
    """Sweep one UTC day of call records for every profile. Returns rows inserted.

    Returns 0 harmlessly when the app-only credential is missing or the
    permission isn't consented — Graph answers 403 and walk() yields nothing.
    """
    token = await acquire_app_token()
    if not token:
        return 0

    async with AsyncSessionLocal() as db:
        profiles = (await db.execute(select(Profile))).scalars().all()
    # Call records identify people by Entra oid, which is what teams_user_id holds.
    by_oid = {p.teams_user_id: str(p.id) for p in profiles if p.teams_user_id}
    if not by_oid:
        return 0

    async with httpx.AsyncClient(timeout=30) as client:
        events = await fetch_call_events(client, token, by_oid, day)
    return sum([await ingest(event) for event in events])
