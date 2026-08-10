"""Teams call records connector — attribution, meeting skipping, duration."""
from datetime import datetime, timezone

import pytest

from app.backfill.teams_calls import call_event, day_params, fetch_call_events
from tests.graph_fakes import graph_client

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
    assert ev["raw_payload"]["people"] == ["dan.okoye@quadrant.com"]


_JOIN_URL = "https://teams.microsoft.com/l/x"


def test_meeting_already_on_your_calendar_is_skipped():
    """A joinWebUrl the calendar connector already stored would double the day."""
    assert call_event(PROFILE, _record(joinWebUrl=_JOIN_URL), _PARTICIPANTS,
                      ME_OID, {_JOIN_URL}) is None


def test_meeting_you_were_never_invited_to_is_kept():
    """Joined by link, or pulled into a call already running: no invite reached
    your calendar, so nothing else records it. Skipping it lost the call."""
    ev = call_event(PROFILE, _record(joinWebUrl=_JOIN_URL), _PARTICIPANTS,
                    ME_OID, set())
    assert ev is not None and ev["title"] == "Dan Okoye"


def test_someone_elses_calendar_does_not_suppress_yours():
    """known_urls is per profile — an invite Dan accepted is not one you have."""
    assert call_event(PROFILE, _record(joinWebUrl=_JOIN_URL), _PARTICIPANTS,
                      ME_OID, {"https://teams.microsoft.com/l/other"}) is not None


def _session(oid, name, start, end, role="caller"):
    return {"startDateTime": start, "endDateTime": end,
            "segments": [{"startDateTime": start, "endDateTime": end}],
            role: {"identity": {"user": {"id": oid, "displayName": name}}}}


def test_join_time_is_yours_not_the_calls():
    """Pulled into a call already running, the record's start is somebody else's
    timing — it read as though you had been there the whole time."""
    sessions = [_session(THEM_OID, "Dan Okoye", "2026-07-28T14:20:00Z", "2026-07-28T14:32:00Z"),
                _session(ME_OID, "Sailu", "2026-07-28T14:28:00Z", "2026-07-28T14:32:00Z")]
    ev = call_event(PROFILE, _record(), _PARTICIPANTS, ME_OID, frozenset(), sessions)
    assert ev["occurred_at"] == datetime(2026, 7, 28, 14, 28, tzinfo=timezone.utc)
    assert ev["raw_payload"]["minutes"] == 4
    assert ev["raw_payload"]["own_times"] is True


def test_a_reconnect_reads_as_one_stretch_not_two_calls():
    """Dropping off and rejoining makes a second session; min/max spans both."""
    sessions = [_session(ME_OID, "Sailu", "2026-07-28T14:20:00Z", "2026-07-28T14:22:00Z"),
                _session(ME_OID, "Sailu", "2026-07-28T14:23:00Z", "2026-07-28T14:32:00Z")]
    ev = call_event(PROFILE, _record(), _PARTICIPANTS, ME_OID, frozenset(), sessions)
    assert ev["occurred_at"] == datetime(2026, 7, 28, 14, 20, tzinfo=timezone.utc)
    assert ev["raw_payload"]["minutes"] == 12


def test_callee_sessions_count_too():
    sessions = [_session(ME_OID, "Sailu", "2026-07-28T14:25:00Z", "2026-07-28T14:32:00Z",
                         role="callee")]
    ev = call_event(PROFILE, _record(), _PARTICIPANTS, ME_OID, frozenset(), sessions)
    assert ev["raw_payload"]["minutes"] == 7


def test_without_sessions_it_falls_back_to_the_records_own_times():
    """Sessions can be missing or refused; a call must still land, flagged as
    the call's timing rather than yours."""
    ev = call_event(PROFILE, _record(), _PARTICIPANTS, ME_OID)
    assert ev["occurred_at"] == datetime(2026, 7, 28, 14, 20, tzinfo=timezone.utc)
    assert ev["raw_payload"]["minutes"] == 12
    assert ev["raw_payload"]["own_times"] is False


def test_a_call_with_only_you_is_dropped():
    solo = [_PARTICIPANTS[0]]
    assert call_event(PROFILE, _record(), solo, ME_OID) is None


def test_missing_end_time_yields_zero_minutes_not_a_crash():
    ev = call_event(PROFILE, _record(endDateTime=None), _PARTICIPANTS, ME_OID)
    assert ev["raw_payload"]["minutes"] == 0


def test_day_filter_uses_startdatetime():
    past = datetime(2026, 8, 1, tzinfo=timezone.utc)
    p = day_params("2026-07-28", now=past)
    assert "startDateTime ge 2026-07-28T00:00:00Z" in p["$filter"]
    assert "startDateTime lt 2026-07-29T00:00:00Z" in p["$filter"]


def test_today_is_clamped_to_now_never_the_future():
    """Graph 400s the entire query if the window reaches past the present, so
    polling today with a naive midnight upper bound returns nothing at all."""
    now = datetime(2026, 7, 28, 15, 30, tzinfo=timezone.utc)
    p = day_params("2026-07-28", now=now)
    assert "startDateTime lt 2026-07-28T15:30:00Z" in p["$filter"]


_client = graph_client


@pytest.mark.asyncio
async def test_fetch_expands_each_record_and_attributes_it():
    """List omits participants_v2, so each record needs a second GET."""
    client = _client(
        {"value": [_record()]},
        {**_record(), "participants_v2": [_p(ME_OID, "Sailu", "sailu@quadrant.com"),
                                          _p(THEM_OID, "Dan Okoye", "dan@quadrant.com")]},
        {"sessions": []},
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
        {"sessions": []},
    )
    events = await fetch_call_events(
        client, "tok", {ME_OID: PROFILE, THEM_OID: other_profile}, "2026-07-28")

    assert {e["profile_id"] for e in events} == {PROFILE, other_profile}
    assert {e["title"] for e in events} == {"Dan Okoye", "Sailu"}


@pytest.mark.asyncio
async def test_meeting_records_are_expanded_then_filtered_per_profile():
    """Whether to keep a meeting record depends on which profile it is being
    written for, and that isn't known until participants_v2 comes back — so the
    expand can no longer be skipped on joinWebUrl alone."""
    expanded = {**_record(joinWebUrl=_JOIN_URL),
                "participants_v2": [_p(ME_OID, "Sailu", "sailu@quadrant.com"),
                                    _p(THEM_OID, "Dan Okoye", "dan@quadrant.com")]}
    client = _client({"value": [_record(joinWebUrl=_JOIN_URL)]}, expanded, {"sessions": []})
    kept = await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28")
    assert len(kept) == 1

    client = _client({"value": [_record(joinWebUrl=_JOIN_URL)]}, expanded, {"sessions": []})
    dropped = await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28",
                                      {PROFILE: {_JOIN_URL}})
    assert dropped == []


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
    assert await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28") == []


@pytest.mark.asyncio
async def test_missing_permission_yields_nothing_without_raising():
    client = _client({"error": {"code": "Forbidden"}}, status=403)
    assert await fetch_call_events(client, "tok", {ME_OID: PROFILE}, "2026-07-28") == []
