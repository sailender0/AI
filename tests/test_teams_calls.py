"""Teams call records connector — attribution, meeting skipping, duration."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.backfill.teams_calls import call_event, day_params, fetch_call_events

PROFILE = "11111111-1111-1111-1111-111111111111"
ME_OID = "oid-me"
THEM_OID = "oid-dan"


def _p(oid, name, upn):
    return {"id": oid, "identity": {"user": {"id": oid, "displayName": name,
                                             "userPrincipalName": upn}}}


_PARTICIPANTS = [
    {"oid": ME_OID, "name": "Sailu", "upn": "sailu@quadrant.com"},
    {"oid": THEM_OID, "name": "Dan Okoye", "upn": "Dan.Okoye@Quadrant.com"},
]


def _record(**over):
    r = {
        "id": "e523d2ed-2966-4b6b-925b-754a88034cc5",
        "type": "peerToPeer",
        "startDateTime": "2026-07-28T14:20:00Z",
        "endDateTime": "2026-07-28T14:32:00Z",
    }
    r.update(over)
    return r


def test_call_files_under_the_other_participant_with_duration():
    ev = call_event(PROFILE, _record(), _PARTICIPANTS, ME_OID)
    assert ev["title"] == "Dan Okoye"
    assert ev["raw_payload"]["minutes"] == 12
    # Keyed by address, lowercased — the same key mail and calendar use.
    assert ev["raw_payload"]["people"] == ["dan.okoye@quadrant.com"]


def test_scheduled_meetings_are_skipped():
    """A record with a joinWebUrl is a meeting the calendar connector already
    stored; writing it again would double every meeting on the day."""
    assert call_event(PROFILE, _record(joinWebUrl="https://teams.microsoft.com/l/x"),
                      _PARTICIPANTS, ME_OID) is None


def test_a_call_with_only_you_is_dropped():
    solo = [_PARTICIPANTS[0]]
    assert call_event(PROFILE, _record(), solo, ME_OID) is None


def test_missing_end_time_yields_zero_minutes_not_a_crash():
    ev = call_event(PROFILE, _record(endDateTime=None), _PARTICIPANTS, ME_OID)
    assert ev["raw_payload"]["minutes"] == 0


def test_day_filter_uses_startdatetime():
    past = datetime(2026, 8, 1, tzinfo=timezone.utc)      # the day is fully over
    p = day_params("2026-07-28", now=past)
    assert "startDateTime ge 2026-07-28T00:00:00Z" in p["$filter"]
    assert "startDateTime lt 2026-07-29T00:00:00Z" in p["$filter"]


def test_today_is_clamped_to_now_never_the_future():
    """Graph 400s the entire query if the window reaches past the present, so
    polling today with a naive midnight upper bound returns nothing at all."""
    now = datetime(2026, 7, 28, 15, 30, tzinfo=timezone.utc)
    p = day_params("2026-07-28", now=now)
    assert "startDateTime lt 2026-07-28T15:30:00Z" in p["$filter"]


# ── Fetch: list then expand ───────────────────────────────────────────────────

def _client(*payloads, status=200):
    def resp(p):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = p
        return r
    c = MagicMock()
    c.get = AsyncMock(side_effect=[resp(p) for p in payloads])
    return c


@pytest.mark.asyncio
async def test_fetch_expands_each_record_and_attributes_it():
    """List omits participants_v2, so each record needs a second GET."""
    client = _client(
        {"value": [_record()]},
        {**_record(), "participants_v2": [_p(ME_OID, "Sailu", "sailu@quadrant.com"),
                                          _p(THEM_OID, "Dan Okoye", "dan@quadrant.com")]},
    )
    events = await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28")

    assert len(events) == 1
    assert events[0]["profile_id"] == PROFILE
    assert events[0]["title"] == "Dan Okoye"


@pytest.mark.asyncio
async def test_both_participants_get_their_own_row():
    """Records are tenant-wide — one call is on two people's calendars."""
    other_profile = "22222222-2222-2222-2222-222222222222"
    client = _client(
        {"value": [_record()]},
        {**_record(), "participants_v2": [_p(ME_OID, "Sailu", "sailu@quadrant.com"),
                                          _p(THEM_OID, "Dan Okoye", "dan@quadrant.com")]},
    )
    events = await fetch_call_events(
        client, "tok", {ME_OID: PROFILE, THEM_OID: other_profile}, "2026-07-28")

    assert {e["profile_id"] for e in events} == {PROFILE, other_profile}
    assert {e["title"] for e in events} == {"Dan Okoye", "Sailu"}


@pytest.mark.asyncio
async def test_meetings_are_skipped_without_spending_an_expand_call():
    client = _client({"value": [_record(joinWebUrl="https://teams.microsoft.com/l/x")]})
    assert await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28") == []
    assert client.get.await_count == 1     # the list only — no expand


@pytest.mark.asyncio
async def test_people_outside_the_app_are_ignored():
    """A colleague with no profile here isn't ours to write a calendar row for."""
    client = _client(
        {"value": [_record()]},
        {**_record(), "participants_v2": [_p(ME_OID, "Sailu", "sailu@quadrant.com"),
                                          _p(THEM_OID, "Dan Okoye", "dan@quadrant.com")]},
    )
    events = await fetch_call_events(client, "tok", {}, "2026-07-28")
    assert events == []


@pytest.mark.asyncio
async def test_pstn_participants_have_no_user_identity():
    """A phone caller carries a number, not a colleague — keep them out of the roster."""
    phone = {"id": "+15551234", "identity": {"phone": {"id": "+15551234"}, "user": None}}
    client = _client(
        {"value": [_record()]},
        {**_record(), "participants_v2": [_p(ME_OID, "Sailu", "sailu@quadrant.com"), phone]},
    )
    # Only you resolve to a user, so there's no counterparty to file it under.
    assert await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28") == []


@pytest.mark.asyncio
async def test_missing_permission_yields_nothing_without_raising():
    client = _client({"error": {"code": "Forbidden"}}, status=403)
    assert await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28") == []
