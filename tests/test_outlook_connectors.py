"""Outlook mail and calendar connectors — direction, filing, and what's stored.

Mail fixtures carry a subject on purpose: Mail.ReadBasic returns subjects, and
the spec is addresses and times only. If a subject ever reaches an event, these
fail. Meeting subjects ARE stored — that is the one deliberate exception.
"""
from datetime import datetime, timezone

import pytest

from app.backfill.outlook_calendar import meeting_event
from app.backfill.outlook_calendar import day_params as cal_params
from app.backfill.outlook_calendar import fetch_meeting_events, headers_for
from app.backfill.outlook_mail import day_params as mail_params
from app.backfill.outlook_mail import fetch_mail_events, mail_event
from tests.graph_fakes import graph_client

PROFILE = "11111111-1111-1111-1111-111111111111"
ME = "sailu@quadrant.com"
SUBJECT = "Q3 headcount plan — confidential"


def _mail(sender=ME, to=("priya@quadrant.com",), **over):
    m = {
        "id": "AAMkAGI1", "subject": SUBJECT,
        "from": {"emailAddress": {"address": sender, "name": "Someone"}},
        "toRecipients": [{"emailAddress": {"address": a, "name": a.split("@")[0]}} for a in to],
        "sentDateTime": "2026-07-28T09:12:00Z",
        "receivedDateTime": "2026-07-28T09:12:04Z",
    }
    m.update(over)
    return m


def test_mail_from_you_is_sent_and_files_under_the_recipient():
    ev = mail_event(PROFILE, _mail(sender=ME), ME)
    assert ev["event_type"] == "mail_sent"
    assert ev["title"] == "priya@quadrant.com"
    assert ev["raw_payload"]["people"] == ["priya@quadrant.com"]


def test_mail_from_someone_else_is_received():
    ev = mail_event(PROFILE, _mail(sender="dan@quadrant.com"), ME)
    assert ev["event_type"] == "mail_received"
    assert ev["title"] == "dan@quadrant.com"


def test_direction_ignores_address_casing():
    ev = mail_event(PROFILE, _mail(sender="SAILU@Quadrant.com"), ME)
    assert ev["event_type"] == "mail_sent"


def test_extra_recipients_are_counted_not_listed():
    ev = mail_event(PROFILE, _mail(to=("a@x.com", "b@x.com", "c@x.com")), ME)
    assert ev["title"] == "a@x.com"
    assert ev["raw_payload"]["extra_recipients"] == 2


def test_subject_is_never_stored():
    """Mail.ReadBasic returns subjects. The calendar shows who and when."""
    for sender in (ME, "dan@quadrant.com"):
        assert SUBJECT not in str(mail_event(PROFILE, _mail(sender=sender), ME))


def test_sent_mail_with_no_recipient_is_dropped():
    assert mail_event(PROFILE, _mail(to=()), ME) is None


def test_mail_query_selects_only_metadata():
    p = mail_params("2026-07-28")
    assert "subject" not in p["$select"] and "body" not in p["$select"]
    assert "ge 2026-07-28T00:00:00Z" in p["$filter"]
    assert "lt 2026-07-29T00:00:00Z" in p["$filter"]


def test_mail_day_is_the_profiles_local_day_not_a_utc_one():
    """A local date pasted into a `...Z` literal queries the wrong window: for a
    zone behind UTC it dropped everything after ~17:00 local until the next day."""
    p = mail_params("2026-07-28", "America/Los_Angeles")
    assert "ge 2026-07-28T07:00:00Z" in p["$filter"]
    assert "lt 2026-07-29T07:00:00Z" in p["$filter"]


def _meeting(**over):
    m = {
        "id": "AAMkEvent1", "subject": "Connector design review",
        "start": {"dateTime": "2026-07-28T10:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-07-28T10:45:00.0000000", "timeZone": "UTC"},
        "organizer": {"emailAddress": {"address": "priya@quadrant.com", "name": "Priya Nair"}},
        "attendees": [
            {"emailAddress": {"address": ME, "name": "Sailu"}},
            {"emailAddress": {"address": "dan@quadrant.com", "name": "Dan Okoye"}},
        ],
        "responseStatus": {"response": "accepted"},
        "isCancelled": False,
    }
    m.update(over)
    return m


def test_meeting_keeps_subject_duration_and_rsvp():
    ev = meeting_event(PROFILE, _meeting(), ME)
    assert ev["title"] == "Connector design review"
    assert ev["raw_payload"]["minutes"] == 45
    assert ev["raw_payload"]["rsvp"] == "accepted"


def test_meeting_roster_excludes_you():
    """You are not your own counterparty — filtering to yourself is meaningless."""
    ev = meeting_event(PROFILE, _meeting(), ME)
    names = [a["name"] for a in ev["raw_payload"]["attendees"]]
    assert names == ["Dan Okoye"]
    assert ev["raw_payload"]["organizer"]["name"] == "Priya Nair"


def test_meeting_matches_the_person_filter_on_any_participant():
    """Organizer first, then attendees — so 'with Dan' finds a meeting Priya ran."""
    people = meeting_event(PROFILE, _meeting(), ME)["raw_payload"]["people"]
    assert people == ["priya@quadrant.com", "dan@quadrant.com"]


def test_cancelled_meetings_are_dropped():
    assert meeting_event(PROFILE, _meeting(isCancelled=True), ME) is None


def test_meeting_you_organised_has_no_organizer_counterparty():
    ev = meeting_event(PROFILE, _meeting(
        organizer={"emailAddress": {"address": ME, "name": "Sailu"}}), ME)
    assert ev["raw_payload"]["organizer"] is None
    assert ev["raw_payload"]["people"] == ["dan@quadrant.com"]


def test_calendar_query_excludes_the_invite_body():
    """Invite descriptions carry agendas and dial-ins — never selected."""
    p = cal_params("2026-07-28", "UTC")
    assert "body" not in p["$select"]
    assert p["startDateTime"] == "2026-07-28T00:00:00"
    assert p["endDateTime"] == "2026-07-29T00:00:00"


def test_calendar_requests_times_in_the_users_timezone():
    """Otherwise a 09:00 local meeting comes back as UTC and reads at the wrong hour."""
    assert 'outlook.timezone="Asia/Kolkata"' in headers_for("tok", "Asia/Kolkata")["Prefer"]


def test_meeting_is_stored_as_a_real_utc_instant():
    """The Prefer header makes Graph send local wall-clock with no offset. Storing
    that naive value made the reader treat 10:00 PDT as 10:00 UTC and render the
    meeting 7 hours out — every other source stores true UTC."""
    ev = meeting_event(PROFILE, _meeting(), ME, "America/Los_Angeles")
    assert ev["occurred_at"] == datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc)
    assert ev["raw_payload"]["minutes"] == 45


_client = graph_client


@pytest.mark.asyncio
async def test_fetch_mail_maps_both_directions():
    client = _client({"value": [_mail(sender=ME), _mail(id="2", sender="dan@quadrant.com")]})
    events = await fetch_mail_events(client, "tok", PROFILE, ME, "2026-07-28")
    assert [e["event_type"] for e in events] == ["mail_sent", "mail_received"]
    assert SUBJECT not in str(events)


@pytest.mark.asyncio
async def test_fetch_meetings_skips_cancelled():
    client = _client({"value": [_meeting(), _meeting(id="x", isCancelled=True)]})
    events = await fetch_meeting_events(client, "tok", PROFILE, ME, "2026-07-28", "UTC")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_missing_scope_yields_nothing_without_raising():
    """403 before consent must no-op, not blow up the hourly sweep."""
    client = _client({"error": {"code": "Forbidden"}}, status=403)
    assert await fetch_mail_events(client, "tok", PROFILE, ME, "2026-07-28") == []
