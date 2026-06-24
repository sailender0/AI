"""
Tests for backfill_teams_delta — the multi-page Graph delta sync that runs
when a Teams subscription reports missed notifications.

Mocks: Redis (get/set), acquire_delegated_token, httpx.AsyncClient, and ingest.
No real network or DB calls are made.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROFILE = "profile-teams-backfill"

_BOT_ID = "bot-sp-id-999"

_MSG = {
    "id": "msg-1",
    "createdDateTime": "2026-06-23T10:00:00Z",
    "body": {"content": "Hello"},
    "from": {"user": {"id": "human-user-1", "displayName": "Dev"}},
    "channelIdentity": {"channelId": "ch-1", "teamId": "team-1"},
}


def _mock_redis(delta_link: str | None = None):
    r = AsyncMock()
    r.get = AsyncMock(return_value=delta_link)
    r.set = AsyncMock()
    return r


def _mock_http_response(data: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=data)
    return resp


def _patch_http(responses: list):
    """Patch httpx.AsyncClient so successive .get() calls return each response in order."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=responses)
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
    return mock_cls, mock_client


async def test_no_token_returns_early():
    from app.webhooks.receivers.teams import backfill_teams_delta

    redis = _mock_redis()
    mock_cls, mock_client = _patch_http([])

    with patch("app.storage.redis_client.get_redis", return_value=redis), \
         patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value=None)), \
         patch("app.webhooks.receivers.teams.httpx.AsyncClient", mock_cls), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await backfill_teams_delta(PROFILE)

    mock_client.get.assert_not_called()
    mock_ingest.assert_not_called()


async def test_single_page_with_delta_link_ingests_messages():
    from app.webhooks.receivers.teams import backfill_teams_delta

    redis = _mock_redis()
    page = {"value": [_MSG], "@odata.deltaLink": "https://graph.microsoft.com/delta?token=abc"}
    mock_cls, mock_client = _patch_http([_mock_http_response(page)])

    with patch("app.storage.redis_client.get_redis", return_value=redis), \
         patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok")), \
         patch("app.webhooks.receivers.teams.httpx.AsyncClient", mock_cls), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await backfill_teams_delta(PROFILE)

    mock_ingest.assert_called_once()
    event = mock_ingest.call_args[0][0]
    assert event["source"] == "teams_subscription"
    assert event["profile_id"] == PROFILE


async def test_delta_link_saved_to_redis():
    from app.webhooks.receivers.teams import backfill_teams_delta

    redis = _mock_redis()
    delta_url = "https://graph.microsoft.com/delta?token=saved"
    page = {"value": [], "@odata.deltaLink": delta_url}
    mock_cls, _ = _patch_http([_mock_http_response(page)])

    with patch("app.storage.redis_client.get_redis", return_value=redis), \
         patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok")), \
         patch("app.webhooks.receivers.teams.httpx.AsyncClient", mock_cls), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()):
        await backfill_teams_delta(PROFILE)

    redis.set.assert_called_once()
    key, value = redis.set.call_args[0]
    assert f"teams_delta_link:{PROFILE}" == key
    assert value == delta_url


async def test_cached_delta_link_used_as_initial_url():
    from app.webhooks.receivers.teams import backfill_teams_delta

    cached_url = "https://graph.microsoft.com/delta?token=cached"
    redis = _mock_redis(delta_link=cached_url)
    page = {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/delta?token=new"}
    mock_cls, mock_client = _patch_http([_mock_http_response(page)])

    with patch("app.storage.redis_client.get_redis", return_value=redis), \
         patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok")), \
         patch("app.webhooks.receivers.teams.httpx.AsyncClient", mock_cls), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()):
        await backfill_teams_delta(PROFILE)

    first_call_url = mock_client.get.call_args_list[0][0][0]
    assert first_call_url == cached_url


async def test_bot_messages_filtered_out():
    from app.webhooks.receivers.teams import backfill_teams_delta

    bot_msg = {**_MSG, "from": {"user": {"id": _BOT_ID, "displayName": "Bot"}}}
    redis = _mock_redis()
    page = {"value": [bot_msg], "@odata.deltaLink": "https://graph.microsoft.com/delta?token=x"}
    mock_cls, _ = _patch_http([_mock_http_response(page)])

    with patch("app.storage.redis_client.get_redis", return_value=redis), \
         patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok")), \
         patch("app.webhooks.receivers.teams.httpx.AsyncClient", mock_cls), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest, \
         patch("app.webhooks.receivers.teams.settings") as mock_settings:
        mock_settings.BOT_SERVICE_PRINCIPAL_ID = _BOT_ID
        await backfill_teams_delta(PROFILE)

    mock_ingest.assert_not_called()


async def test_pagination_follows_next_link():
    from app.webhooks.receivers.teams import backfill_teams_delta

    next_url = "https://graph.microsoft.com/delta?$skiptoken=page2"
    page1 = {"value": [_MSG], "@odata.nextLink": next_url}
    page2 = {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/delta?token=final"}
    mock_cls, mock_client = _patch_http([
        _mock_http_response(page1),
        _mock_http_response(page2),
    ])
    redis = _mock_redis()

    with patch("app.storage.redis_client.get_redis", return_value=redis), \
         patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok")), \
         patch("app.webhooks.receivers.teams.httpx.AsyncClient", mock_cls), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await backfill_teams_delta(PROFILE)

    assert mock_client.get.call_count == 2
    second_url = mock_client.get.call_args_list[1][0][0]
    assert second_url == next_url
    mock_ingest.assert_called_once()  # only page1 had a message


async def test_http_error_breaks_loop():
    from app.webhooks.receivers.teams import backfill_teams_delta

    redis = _mock_redis()
    mock_cls, mock_client = _patch_http([_mock_http_response({}, status=500)])

    with patch("app.storage.redis_client.get_redis", return_value=redis), \
         patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok")), \
         patch("app.webhooks.receivers.teams.httpx.AsyncClient", mock_cls), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await backfill_teams_delta(PROFILE)

    mock_client.get.assert_called_once()
    mock_ingest.assert_not_called()
