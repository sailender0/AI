"""Outlook calendar -> activity events. Meeting name, times, invite list, RSVP.

Delegated `Calendars.Read`. Uses /me/calendarView rather than /me/events: events
returns the recurring *master*, so a daily standup would appear once ever, while
calendarView expands recurrences into the real occurrences a day actually had.

The meeting subject is the one piece of user-authored content this app stores,
and it is in scope deliberately — a calendar of unnamed blocks is useless.
Bodies (the invite description, which often carries agendas and dial-ins) are
excluded by $select.

What this CANNOT tell you is whether anyone turned up: responseStatus is intent
recorded days earlier. Verified attendance needs callRecord.participants[] —
application-only CallRecords.Read.All, a separate connector.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.auth.sso import acquire_delegated_token
from app.backfill import GRAPH, addr_of, make_event, parse_iso, person_key, walk
from app.storage.models import Profile
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import ingest

logger = logging.getLogger(__name__)

_FIELDS = ("id,subject,start,end,organizer,attendees,responseStatus,"
           "isCancelled,isAllDay,onlineMeeting")

_MAX_ATTENDEES = 40


def day_params(day: str, tz_name: str) -> dict:
    """calendarView over one local day. The window is sent as local wall-clock
    time and interpreted in the Prefer timezone set by the caller's headers."""
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return {
        "$top": 50,
        "$select": _FIELDS,
        "$orderby": "start/dateTime",
        "startDateTime": f"{day}T00:00:00",
        "endDateTime": f"{nxt}T00:00:00",
    }


def headers_for(token: str, tz_name: str) -> dict:
    """Ask Graph to return start/end in the user's own timezone, so a meeting at
    09:00 local reads as 09:00 rather than needing conversion here."""
    return {"Authorization": f"Bearer {token}",
            "Prefer": f'outlook.timezone="{tz_name or "UTC"}"'}


def as_utc(raw_ts, tz_name: str) -> datetime:
    """A Graph start/end -> a real UTC instant.

    The Prefer header makes Graph answer in the user's zone and it sends those
    times with NO offset ("2026-07-29T07:30:00.0000000"), so parse_iso yields a
    naive datetime. Stored as-is it was read back as UTC by
    calendar_activity._local(), putting every meeting out by the whole offset — a
    07:30 standup rendered at 00:30, and meetings near midnight landed on the
    wrong day. Every other source stores true UTC; stamp the requested zone on
    before converting so this one does too.
    """
    ts = parse_iso(raw_ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo(tz_name or "UTC"))
    return ts.astimezone(timezone.utc)


def _minutes(event: dict) -> int:
    start = parse_iso((event.get("start") or {}).get("dateTime"))
    end = parse_iso((event.get("end") or {}).get("dateTime"))
    return max(int((end - start).total_seconds() // 60), 0)


def meeting_event(profile_id: str, event: dict, self_email: str,
                  tz_name: str = "UTC") -> dict | None:
    """One calendarView event -> event dict, or None if it isn't a real meeting."""
    if event.get("isCancelled"):
        return None
    start = (event.get("start") or {}).get("dateTime")
    if not start:
        return None

    org_addr, org_name = addr_of(event.get("organizer"))
    me = (self_email or "").lower()

    attendees, people = [], []
    for entry in (event.get("attendees") or [])[:_MAX_ATTENDEES]:
        addr, name = addr_of(entry)
        if not addr or addr.lower() == me:
            continue
        attendees.append({"id": person_key(addr, addr), "name": name or addr})
        people.append(person_key(addr, addr))

    organizer = None
    if org_addr and org_addr.lower() != me:
        organizer = {"id": person_key(org_addr, org_addr), "name": org_name or org_addr}
        people.insert(0, organizer["id"])

    return make_event(
        profile_id=profile_id,
        source="outlook_calendar",
        event_type="meeting",
        source_event_id=str(event["id"]),
        title=event.get("subject") or "(no subject)",
        occurred_at=as_utc(start, tz_name),
        workspace="all-day" if event.get("isAllDay") else None,
        raw={
            "user_id": (organizer or {}).get("id"),
            "people": list(dict.fromkeys(people)),
            "organizer": organizer,
            "attendees": attendees,
            "minutes": _minutes(event),
            "rsvp": (event.get("responseStatus") or {}).get("response") or "none",
            "join_url": (event.get("onlineMeeting") or {}).get("joinUrl"),
        },
    )


async def fetch_meeting_events(client, token: str, profile_id: str, self_email: str,
                               day: str, tz_name: str) -> list[dict]:
    """Every meeting on `day`, as event dicts."""
    events = await walk(client, f"{GRAPH}/me/calendarView",
                        headers_for(token, tz_name), day_params(day, tz_name))
    out = []
    for event in events:
        if not event.get("id"):
            continue
        mapped = meeting_event(profile_id, event, self_email, tz_name)
        if mapped:
            out.append(mapped)
    return out


async def backfill_calendar_day(profile_id: str, day: str) -> int:
    """Ingest one day of meetings for one profile. Returns rows newly inserted."""
    async with AsyncSessionLocal() as db:
        profile = (await db.execute(
            select(Profile).where(Profile.id == profile_id)
        )).scalar_one_or_none()

    if not profile:
        return 0

    token = await acquire_delegated_token(profile_id)
    if not token:
        return 0

    async with httpx.AsyncClient(timeout=30) as client:
        events = await fetch_meeting_events(client, token, profile_id,
                                            profile.email or "", day,
                                            profile.timezone or "UTC")
    return sum([await ingest(event) for event in events])
