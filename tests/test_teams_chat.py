"""Teams chat connector — mapping and fetching.

The load-bearing assertion in this file is that no message body ever reaches an
event dict. Graph returns body.content on every message (/chats/{id}/messages has
no $select), so the fixtures carry real body text on purpose: if anyone routes
chat through normalize() or passes `raw` verbatim, these fail.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.backfill.graph_poll import poll_days
from app.backfill.teams_chat import (
    chat_counterparty,
    chat_message_event,
    day_params,
    fetch_chat_events,
)
from tests.graph_fakes import graph_client, graph_resp

PROFILE = "11111111-1111-1111-1111-111111111111"
SELF = "oid-self"
OTHER = "oid-userb"
OTHER_KEY = "user.b@example.com"
SECRET = "the acquisition closes friday"

_ONE_ON_ONE = {
    "id": "19:self_userb@unq.gbl.spaces",
    "chatType": "oneOnOne",
    "members": [
        {"userId": SELF, "displayName": "You", "email": "me@example.com"},
        {"userId": OTHER, "displayName": "User B", "email": "User.B@example.com"},
    ],
}
_GROUP = {
    "id": "19:group@thread.v2",
    "chatType": "group",
    "topic": "Design Guild",
    "members": [
        {"userId": SELF, "displayName": "You", "email": "me@example.com"},
        {"userId": OTHER, "displayName": "User B", "email": "User.B@example.com"},
        {"userId": "oid-priya", "displayName": "Priya Nair", "email": "priya@example.com"},
    ],
}


def _msg(sender=OTHER, name="User B", **over):
    m = {
        "id": "1622853091207",
        "messageType": "message",
        "isDeleted": False,
        "createdDateTime": "2026-07-28T09:00:00.000Z",
        "lastModifiedDateTime": "2026-07-28T09:00:00.000Z",
        "from": {"application": None, "device": None,
                 "user": {"id": sender, "displayName": name, "userIdentityType": "aadUser"}},
        "body": {"contentType": "text", "content": SECRET},
    }
    m.update(over)
    return m


def test_body_never_reaches_the_event():
    event = chat_message_event(PROFILE, _ONE_ON_ONE, _msg(), SELF)
    assert SECRET not in str(event)
    assert event["raw_payload"] == {"user_id": OTHER_KEY, "people": [OTHER_KEY],
                                    "from_self": False, "chat_type": "oneOnOne"}


def test_title_is_a_person_not_a_message():
    event = chat_message_event(PROFILE, _ONE_ON_ONE, _msg(), SELF)
    assert event["title"] == "User B"
    assert event["occurred_at"].hour == 9
    assert event["source_event_id"] == "1622853091207"


def test_bot_message_dropped():
    bot = _msg()
    bot["from"] = {"user": None, "application": {"id": "b1", "displayName": "talla",
                                                 "applicationIdentityType": "bot"}}
    assert chat_message_event(PROFILE, _ONE_ON_ONE, bot, SELF) is None


def test_system_event_message_dropped():
    sys_msg = _msg(messageType="systemEventMessage")
    sys_msg["from"] = None
    assert chat_message_event(PROFILE, _ONE_ON_ONE, sys_msg, SELF) is None


def test_deleted_message_dropped():
    assert chat_message_event(PROFILE, _ONE_ON_ONE, _msg(isDeleted=True), SELF) is None


def test_their_message_files_under_the_sender():
    who = chat_counterparty(_ONE_ON_ONE, _msg(), SELF)
    assert who == {"id": OTHER_KEY, "name": "User B", "from_self": False}


def test_your_own_reply_files_under_the_other_member():
    """Not under you — otherwise filtering to User B would hide half the thread."""
    who = chat_counterparty(_ONE_ON_ONE, _msg(sender=SELF, name="You"), SELF)
    assert who == {"id": OTHER_KEY, "name": "User B", "from_self": True}


def test_their_message_in_a_group_files_under_the_sender():
    who = chat_counterparty(_GROUP, _msg(), SELF)
    assert who["id"] == OTHER_KEY and who["from_self"] is False


def test_person_key_falls_back_to_the_oid_without_an_address():
    """The members expansion caps at 25, so a sender in a large group chat may
    not be in the roster. An oid still groups them consistently."""
    big = {"id": "19:big@thread.v2", "chatType": "group", "members": []}
    who = chat_counterparty(big, _msg(), SELF)
    assert who["id"] == OTHER


def test_your_own_group_post_has_no_counterparty():
    """A group post is with everyone, so it is with nobody in particular."""
    assert chat_counterparty(_GROUP, _msg(sender=SELF, name="You"), SELF) is None
    assert chat_message_event(PROFILE, _GROUP, _msg(sender=SELF, name="You"), SELF) is None


def test_filter_and_orderby_name_the_same_property():
    """Graph ignores $filter otherwise — 200 OK with every message, no error."""
    p = day_params("2026-07-28")
    assert "lastModifiedDateTime" in p["$orderby"]
    assert p["$filter"].count("lastModifiedDateTime") == 2
    assert "createdDateTime" not in p["$filter"]


def test_day_window_is_half_open():
    p = day_params("2026-07-28")
    assert "gt 2026-07-28T00:00:00Z" in p["$filter"]
    assert "lt 2026-07-29T00:00:00Z" in p["$filter"]


def test_day_window_follows_the_profiles_zone():
    """Ahead of UTC, a local date in a `...Z` literal skipped the early morning
    entirely — and nothing re-polls a past day, so those messages were lost."""
    p = day_params("2026-07-28", "Asia/Kolkata")
    assert "gt 2026-07-27T18:30:00Z" in p["$filter"]
    assert "lt 2026-07-28T18:30:00Z" in p["$filter"]


def test_poll_covers_today_only_during_the_day():
    from datetime import datetime
    assert poll_days(datetime(2026, 7, 28, 14, 0)) == ["2026-07-28"]


def test_poll_also_covers_yesterday_just_after_midnight():
    """A 23:58 message can land after the last run of that day; re-polling is
    free because ingest() dedups on the Graph message id."""
    from datetime import datetime
    assert poll_days(datetime(2026, 7, 28, 1, 0)) == ["2026-07-28", "2026-07-27"]


_resp, _client = graph_resp, graph_client


@pytest.mark.asyncio
async def test_fetch_returns_one_row_per_message():
    """Two messages from the same person, hours apart, stay two rows."""
    client = _client(
        {"value": [_ONE_ON_ONE]},
        {"value": [_msg(), _msg(id="2", createdDateTime="2026-07-28T17:00:00.000Z")]},
    )
    events = await fetch_chat_events(client, "tok", PROFILE, SELF, "2026-07-28")

    assert len(events) == 2
    assert [e["title"] for e in events] == ["User B", "User B"]
    assert {e["occurred_at"].hour for e in events} == {9, 17}
    assert SECRET not in str(events)


@pytest.mark.asyncio
async def test_message_edited_today_but_written_earlier_is_excluded():
    """The filter is on lastModifiedDateTime, so old-but-edited messages arrive.
    They belong to the day they were written, not the day they were touched."""
    stale = _msg(id="3", createdDateTime="2026-06-01T10:00:00.000Z",
                 lastModifiedDateTime="2026-07-28T11:00:00.000Z")
    client = _client({"value": [_ONE_ON_ONE]}, {"value": [stale]})
    assert await fetch_chat_events(client, "tok", PROFILE, SELF, "2026-07-28") == []


@pytest.mark.asyncio
async def test_missing_scope_yields_nothing_without_raising():
    """Before consent the token lacks Chat.Read and Graph answers 403 — the job
    must no-op rather than blow up, so it can ship ahead of admin approval."""
    client = MagicMock()
    client.get = AsyncMock(return_value=_resp({"error": {"code": "Forbidden"}}, status=403))
    assert await fetch_chat_events(client, "tok", PROFILE, SELF, "2026-07-28") == []


@pytest.mark.asyncio
async def test_paging_follows_nextlink_without_resending_params():
    client = _client(
        {"value": [_ONE_ON_ONE]},
        {"value": [_msg()], "@odata.nextLink": "https://graph.microsoft.com/next"},
        {"value": [_msg(id="9", createdDateTime="2026-07-28T12:00:00.000Z")]},
    )
    events = await fetch_chat_events(client, "tok", PROFILE, SELF, "2026-07-28")

    assert len(events) == 2
    assert client.get.await_args_list[-1].kwargs["params"] is None
