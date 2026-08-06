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
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest

logger = logging.getLogger(__name__)

_LIST_CAP = 100


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


async def known_join_urls() -> dict[str, set[str]]:
    """profile_id -> the meeting join URLs the calendar connector already stored.

    This is what makes the joinWebUrl skip below safe. It has to be per profile:
    a meeting on your calendar is on nobody else's unless they were invited too,
    and skipping a record globally hid every call joined by link from whoever
    lacked the invite.
    """
    out: dict[str, set[str]] = {}
    cursor = activity_events().find(
        {"source": "outlook_calendar", "raw_payload.join_url": {"$nin": [None, ""]}},
        {"profile_id": 1, "raw_payload.join_url": 1},
    )
    async for doc in cursor:
        url = (doc.get("raw_payload") or {}).get("join_url")
        if url:
            out.setdefault(str(doc.get("profile_id")), set()).add(url)
    return out


async def fetch_sessions(client, headers: dict, record_id: str) -> list[dict]:
    """The per-person join/leave times for one call record.

    A second request on purpose: participants_v2 carries identities but no times,
    sessions carry times, and Graph 400s the whole query if you ask for both at
    once ($expand=participants_v2,sessions($expand=segments) is rejected). Only
    called for records that actually produce a row, so the cost is per kept call,
    not per record in the tenant's day.
    """
    try:
        resp = await client.get(f"{GRAPH}/communications/callRecords/{record_id}",
                                headers=headers,
                                params={"$expand": "sessions($expand=segments)"})
    except httpx.HTTPError as exc:
        logger.warning("fetching call sessions failed — %s", type(exc).__name__)
        return []
    if resp.status_code != 200:
        logger.warning("Graph %s fetching call sessions", resp.status_code)
        return []
    return resp.json().get("sessions") or []


def presence(sessions: list[dict], oid: str) -> tuple[datetime | None, datetime | None]:
    """(first join, last leave) for one person across a call's sessions.

    min/max rather than one interval because a reconnect is a new session — drop
    off Wi-Fi and rejoin and you get two, which should read as one stretch of
    attendance, not two calls.
    """
    starts, ends = [], []
    for session in sessions:
        for role in ("caller", "callee"):
            ident = ((session.get(role) or {}).get("identity") or {}).get("user") or {}
            if ident.get("id") != oid or not session.get("startDateTime"):
                continue
            starts.append(parse_iso(session["startDateTime"]))
            if session.get("endDateTime"):
                ends.append(parse_iso(session["endDateTime"]))
    if not starts:
        return None, None
    return min(starts), (max(ends) if ends else None)


def call_event(profile_id: str, record: dict, participants: list[dict],
               self_oid: str, known_urls: set[str] = frozenset(),
               sessions: list[dict] | None = None) -> dict | None:
    """One call record -> an event for one participant's calendar.

    A record with a joinWebUrl is skipped ONLY when this profile already has that
    meeting from the calendar connector, which would otherwise double it. Skipping
    every joinWebUrl record unconditionally was wrong: join a call by link, or get
    pulled into one already running, and there is no invite on your calendar — so
    neither connector kept it and the call vanished.

    occurred_at is when THIS person joined, and minutes is how long they were on —
    both from `sessions`. The record's own start/end describe the call, which for
    anyone who arrived late or left early is somebody else's timing. Falls back to
    the record when sessions are missing, so a call still lands.
    """
    if record.get("joinWebUrl") and record["joinWebUrl"] in known_urls:
        return None

    others = [p for p in participants if p["oid"] != self_oid]
    if not others:
        return None

    joined, left = presence(sessions or [], self_oid)
    started = joined or parse_iso(record.get("startDateTime"))
    ended = left or (parse_iso(record["endDateTime"]) if record.get("endDateTime") else None)
    minutes = max(int((ended - started).total_seconds() // 60), 0) if ended else 0

    keys = [person_key(p["upn"], p["oid"]) for p in others]
    return make_event(
        profile_id=profile_id,
        source="teams_call",
        event_type="call",
        source_event_id=str(record["id"]),
        title=others[0]["name"] or others[0]["upn"] or "Unknown",
        occurred_at=started,
        workspace=record.get("type") or None,
        raw={"user_id": keys[0], "people": keys, "minutes": minutes,
             "participants": [p["name"] for p in others if p["name"]],
             "own_times": joined is not None},
    )


async def already_stored(day_events: str = "teams_call") -> set[str]:
    """Call record ids already ingested, so a re-poll doesn't re-expand them."""
    return set(await activity_events().distinct(
        "source_event_id", {"source": day_events}))


async def fetch_call_events(client, token: str, profiles_by_oid: dict, day: str,
                            known: dict[str, set[str]] | None = None,
                            seen: set[str] | None = None) -> list[dict]:
    """The whole tenant's calls for `day`, as events per participating profile.

    Meeting records can no longer be discarded before the expand: whether to keep
    one depends on which profile is being written, and that needs participants_v2.
    That costs an expand per meeting, so `seen` skips records already ingested —
    without it the hourly sweep re-expands the tenant's whole day every run.
    """
    headers = {"Authorization": f"Bearer {token}"}
    records = await walk(client, f"{GRAPH}/communications/callRecords",
                         headers, day_params(day), cap=_LIST_CAP)

    events = []
    for record in records:
        if not record.get("id") or record["id"] in (seen or set()):
            continue

        try:
            resp = await client.get(f"{GRAPH}/communications/callRecords/{record['id']}",
                                    headers=headers, params={"$expand": "participants_v2"})
        except httpx.HTTPError as exc:
            logger.warning("expanding call record failed — %s", type(exc).__name__)
            continue
        if resp.status_code != 200:
            logger.warning("Graph %s expanding call record", resp.status_code)
            continue
        full = resp.json()

        participants = [p for p in (_identity(e) for e in full.get("participants_v2") or []) if p]
        tracked = [p for p in participants if profiles_by_oid.get(p["oid"])]
        if not tracked or len(participants) < 2:
            continue

        sessions = await fetch_sessions(client, headers, record["id"])
        for person in tracked:
            profile_id = profiles_by_oid[person["oid"]]
            event = call_event(profile_id, full, participants, person["oid"],
                               (known or {}).get(profile_id, frozenset()), sessions)
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
    by_oid = {p.teams_user_id: str(p.id) for p in profiles if p.teams_user_id}
    if not by_oid:
        return 0

    known = await known_join_urls()
    seen = await already_stored()
    async with httpx.AsyncClient(timeout=30) as client:
        events = await fetch_call_events(client, token, by_oid, day, known, seen)
    return sum([await ingest(event) for event in events])
