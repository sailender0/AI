"""Activity calendar — one month grid and one day timeline across both connectors.

Row types come from the event `source`:

    teams_chat        -> chat                (shipped)
    outlook_calendar  -> meeting             (connector not built)
    outlook_mail      -> received / sent     (connector not built)
    teams_call        -> call                (needs CallRecords.Read.All, app-only)

Unmapped sources are ignored, so the git/Jira events sharing this collection
never leak onto the calendar.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.timezone import day_bounds
from app.storage.mongodb import activity_events

TYPES = ["received", "sent", "chat", "meeting", "call"]

_SOURCE_TYPE = {
    "teams_chat": "chat",
    "outlook_calendar": "meeting",
    "teams_call": "call",
}


def row_type(event: dict) -> str | None:
    """Which calendar row type an event renders as, or None to ignore it."""
    source = event.get("source", "")
    if source == "outlook_mail":
        return "sent" if event.get("event_type") == "mail_sent" else "received"
    return _SOURCE_TYPE.get(source)


def month_bounds(month: str, tz_name: str) -> tuple[datetime, datetime]:
    """UTC [start, end) for a local calendar month given as YYYY-MM."""
    tz = ZoneInfo(tz_name or "UTC")
    year, mon = (int(p) for p in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=tz)
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _person(event: dict) -> tuple[str, str] | None:
    """(id, display name) of the counterparty this event is filed under."""
    pid = (event.get("raw_payload") or {}).get("user_id")
    name = event.get("title") or ""
    return (pid, name) if pid and name else None


def _query(profile_id: str, start: datetime, end: datetime, person: str | None) -> dict:
    q: dict = {
        "profile_id": profile_id,
        "occurred_at": {"$gte": start, "$lt": end},
        "source": {"$in": list(_SOURCE_TYPE) + ["outlook_mail"]},
    }
    if person:
        q["raw_payload.people"] = person
    return q


async def build_month(profile_id: str, month: str, tz_name: str,
                      person: str | None = None) -> dict:
    """Per-day type counts for the grid, month totals, and the people seen."""
    start, end = month_bounds(month, tz_name)
    tz = ZoneInfo(tz_name or "UTC")

    days: dict[str, dict[str, int]] = {}
    totals = dict.fromkeys(TYPES, 0)
    people: dict[str, str] = {}

    async for event in activity_events().find(_query(profile_id, start, end, person)):
        kind = row_type(event)
        if not kind:
            continue
        local = _local(event.get("occurred_at"), tz)
        days.setdefault(local.strftime("%Y-%m-%d"), {}).setdefault(kind, 0)
        days[local.strftime("%Y-%m-%d")][kind] += 1
        totals[kind] += 1
        found = _person(event)
        if found:
            people.setdefault(found[0], found[1])

    return {
        "month": month,
        "days": days,
        "totals": totals,
        "people": sorted(({"id": i, "name": n} for i, n in people.items()),
                         key=lambda p: p["name"].lower()),
    }


async def build_day(profile_id: str, date_str: str, tz_name: str,
                    person: str | None = None) -> dict:
    """The timeline for one local day, oldest first."""
    start, end = day_bounds(date_str, tz_name)
    tz = ZoneInfo(tz_name or "UTC")

    items = []
    async for event in activity_events().find(_query(profile_id, start, end, person)).sort("occurred_at", 1):
        kind = row_type(event)
        if not kind:
            continue
        raw = event.get("raw_payload") or {}
        item = {
            "type": kind,
            "time": _local(event.get("occurred_at"), tz).strftime("%H:%M"),
            "title": event.get("title") or "",
            "person_id": raw.get("user_id"),
            "from_self": bool(raw.get("from_self")),
            "context": event.get("workspace") or "",
        }
        if kind == "sent" and raw.get("extra_recipients"):
            item["extra"] = raw["extra_recipients"]
        if kind == "call":
            item["minutes"] = raw.get("minutes") or 0
            item["roster"] = raw.get("participants") or []
        if kind == "meeting":
            item["minutes"] = raw.get("minutes") or 0
            item["rsvp"] = raw.get("rsvp") or "none"
            roster = ([raw["organizer"]] if raw.get("organizer") else []) + (raw.get("attendees") or [])
            item["roster"] = [r["name"] for r in roster if r.get("name")]
            item["organizer"] = (raw.get("organizer") or {}).get("name") or ""
        items.append(item)
    return {"date": date_str, "items": items}


def _local(ts, tz: ZoneInfo) -> datetime:
    """Mongo may hand back naive datetimes; those are UTC by storage convention."""
    if not isinstance(ts, datetime):
        return datetime.now(tz)
    return (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).astimezone(tz)
