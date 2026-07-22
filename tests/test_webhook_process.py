"""
Tests for webhook receiver _process() functions.

ingest() and _resolve_profile() are mocked so no DB, Redis, or MongoDB is required.
The key assertions: correct number of ingest() calls and correct event shape.
"""
import pytest
from unittest.mock import AsyncMock, patch

PROFILE = "profile-abc-123"
TEAMS_PROFILE = "00000000-0000-0000-0000-000000000099"  # valid UUID for Teams clientState validation


# ── GitHub ────────────────────────────────────────────────────────────────────

async def test_github_push_calls_ingest_once():
    from app.webhooks.receivers.github import _process

    body = {
        "sender": {"id": 4242},
        "head_commit": {"message": "Fix auth bug"},
        "repository": {"full_name": "org/repo"},
        "created_at": "2026-06-23T10:00:00Z",
    }

    with patch("app.webhooks.receivers.github._resolve_profile", new=AsyncMock(return_value=PROFILE)) as mock_resolve, \
         patch("app.webhooks.receivers.github.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body, "push")

    mock_resolve.assert_awaited_once_with("4242")   # resolves by actor, not installation
    mock_ingest.assert_called_once()
    event = mock_ingest.call_args[0][0]
    assert event["source"] == "github"
    assert event["profile_id"] == PROFILE
    assert event["event_type"] == "commit"


async def test_github_unresolved_profile_skips_ingest():
    from app.webhooks.receivers.github import _process

    with patch("app.webhooks.receivers.github._resolve_profile", new=AsyncMock(return_value=None)), \
         patch("app.webhooks.receivers.github.ingest", new=AsyncMock()) as mock_ingest:
        await _process({"sender": {"id": 999}}, "push")

    mock_ingest.assert_not_called()


async def test_github_missing_sender_skips_ingest():
    """No actor in the payload → nothing to attribute to → drop (org-noise guard)."""
    from app.webhooks.receivers.github import _process

    with patch("app.webhooks.receivers.github._resolve_profile", new=AsyncMock(return_value=None)) as mock_resolve, \
         patch("app.webhooks.receivers.github.ingest", new=AsyncMock()) as mock_ingest:
        await _process({"repository": {"full_name": "org/repo"}}, "push")

    mock_resolve.assert_awaited_once_with(None)
    mock_ingest.assert_not_called()


async def test_github_pr_merged_event_type():
    from app.webhooks.receivers.github import _process

    body = {
        "sender": {"id": 4242},
        "action": "closed",
        "pull_request": {"id": 1, "title": "Merge feature", "merged": True},
        "repository": {"full_name": "org/repo"},
    }

    with patch("app.webhooks.receivers.github._resolve_profile", new=AsyncMock(return_value=PROFILE)), \
         patch("app.webhooks.receivers.github.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body, "pull_request")

    event = mock_ingest.call_args[0][0]
    assert event["event_type"] == "pr_merged"


async def test_github_uninstall_disconnects_and_skips_ingest():
    """installation/deleted (org-page uninstall) must disconnect, not be ingested
    as activity."""
    from app.webhooks.receivers.github import _process

    body = {"action": "deleted", "installation": {"id": 555}, "sender": {"id": 4242}}

    with patch("app.webhooks.receivers.github.disconnect_installation", new=AsyncMock()) as mock_disc, \
         patch("app.webhooks.receivers.github.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body, "installation")

    mock_disc.assert_awaited_once_with("555")
    mock_ingest.assert_not_called()


async def test_github_two_actors_route_to_their_own_profiles():
    """Two people push to the SAME repo → each event is attributed to its own
    pusher (sender.id), never cross-contaminated onto the other profile."""
    from app.webhooks.receivers.github import _process

    p1, p2 = "profile-p1", "profile-p2"
    actor_to_profile = {"111": p1, "222": p2}

    def push(sender_id, msg):
        return {
            "sender": {"id": sender_id},
            "head_commit": {"message": msg},
            "repository": {"full_name": "acme/webapp"},
            "created_at": "2026-06-23T10:00:00Z",
        }

    with patch("app.webhooks.receivers.github._resolve_profile",
               new=AsyncMock(side_effect=lambda actor_id: actor_to_profile.get(actor_id))), \
         patch("app.webhooks.receivers.github.ingest", new=AsyncMock()) as mock_ingest:
        await _process(push(111, "P1 change"), "push")
        await _process(push(222, "P2 change"), "push")

    assert mock_ingest.call_count == 2
    events = [c[0][0] for c in mock_ingest.call_args_list]
    # Same repo, but each event carries its OWN pusher's profile.
    assert events[0]["profile_id"] == p1
    assert events[1]["profile_id"] == p2
    assert events[0]["workspace"] == events[1]["workspace"] == "acme/webapp"


# ── GitLab ────────────────────────────────────────────────────────────────────

async def test_gitlab_push_one_ingest_per_commit():
    from app.webhooks.receivers.gitlab import _process

    body = {
        "object_kind": "push",
        "project": {"path_with_namespace": "group/project"},
        "user_id": 77,
        "user_username": "sailender",
        "commits": [
            {"id": "c1", "message": "First", "timestamp": "2026-06-23T10:00:00Z"},
            {"id": "c2", "message": "Second", "timestamp": "2026-06-23T10:01:00Z"},
            {"id": "c3", "message": "Third", "timestamp": "2026-06-23T10:02:00Z"},
        ],
    }

    with patch("app.webhooks.receivers.gitlab._resolve_profile", new=AsyncMock(return_value=PROFILE)) as mock_resolve, \
         patch("app.webhooks.receivers.gitlab.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body)

    mock_resolve.assert_awaited_once_with("77")   # by actor (pusher), not project
    assert mock_ingest.call_count == 3
    events = [c[0][0] for c in mock_ingest.call_args_list]
    assert all(e["event_type"] == "commit" for e in events)
    assert all(e["source"] == "gitlab" for e in events)
    # Each event should carry its own commit id
    assert {e["source_event_id"] for e in events} == {"c1", "c2", "c3"}


async def test_gitlab_push_empty_commits_skips_ingest():
    from app.webhooks.receivers.gitlab import _process

    body = {
        "object_kind": "push",
        "project": {"path_with_namespace": "group/project"},
        "user_id": 77,
        "commits": [],
    }

    with patch("app.webhooks.receivers.gitlab._resolve_profile", new=AsyncMock(return_value=PROFILE)), \
         patch("app.webhooks.receivers.gitlab.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body)

    mock_ingest.assert_not_called()


async def test_gitlab_mr_single_ingest():
    from app.webhooks.receivers.gitlab import _process

    body = {
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "group/project"},
        "user": {"id": 88, "username": "sailender"},   # MR/issue/note nest the actor under "user"
        "object_attributes": {"id": 55, "title": "Add feature"},
        "created_at": "2026-06-23T11:00:00Z",
    }

    with patch("app.webhooks.receivers.gitlab._resolve_profile", new=AsyncMock(return_value=PROFILE)) as mock_resolve, \
         patch("app.webhooks.receivers.gitlab.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body)

    mock_resolve.assert_awaited_once_with("88")
    mock_ingest.assert_called_once()
    event = mock_ingest.call_args[0][0]
    assert event["event_type"] == "merge_request"
    assert event["source"] == "gitlab"


async def test_gitlab_unresolved_actor_skips_ingest():
    """Actor isn't one of our connected users → drop (mirrors github/jira)."""
    from app.webhooks.receivers.gitlab import _process

    body = {
        "object_kind": "push",
        "project": {"path_with_namespace": "group/project"},
        "user_id": 999,
        "commits": [{"id": "c1", "message": "x", "timestamp": "2026-06-23T10:00:00Z"}],
    }

    with patch("app.webhooks.receivers.gitlab._resolve_profile", new=AsyncMock(return_value=None)), \
         patch("app.webhooks.receivers.gitlab.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body)

    mock_ingest.assert_not_called()


# ── Teams ─────────────────────────────────────────────────────────────────────

_TEAMS_MESSAGE = {
    "id": "msg-001",
    "createdDateTime": "2026-06-23T10:00:00Z",
    "body": {"content": "Deployed v2 to prod"},
    "from": {"user": {"id": "user-human-123", "displayName": "Sailender"}},
    "channelIdentity": {"channelId": "ch-1", "teamId": "team-1"},
}


async def test_teams_valid_notification_calls_ingest():
    from app.webhooks.receivers.teams import _process_notification

    notification = {"clientState": TEAMS_PROFILE, "resource": "teams/messages/msg-001"}

    with patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok-123")), \
         patch("app.webhooks.receivers.teams._fetch_message", new=AsyncMock(return_value=_TEAMS_MESSAGE)), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await _process_notification(notification)

    mock_ingest.assert_called_once()
    event = mock_ingest.call_args[0][0]
    assert event["source"] == "teams_subscription"
    assert event["profile_id"] == TEAMS_PROFILE


async def test_teams_no_token_skips_ingest():
    from app.webhooks.receivers.teams import _process_notification

    notification = {"clientState": TEAMS_PROFILE, "resource": "teams/messages/msg-001"}

    with patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value=None)), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await _process_notification(notification)

    mock_ingest.assert_not_called()


async def test_teams_no_message_skips_ingest():
    from app.webhooks.receivers.teams import _process_notification

    notification = {"clientState": TEAMS_PROFILE, "resource": "teams/messages/msg-001"}

    with patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok-123")), \
         patch("app.webhooks.receivers.teams._fetch_message", new=AsyncMock(return_value=None)), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await _process_notification(notification)

    mock_ingest.assert_not_called()


async def test_teams_bot_message_skips_ingest():
    """Messages from the bot's service principal must be filtered out."""
    from app.webhooks.receivers.teams import _process_notification

    bot_id = "bot-sp-id-999"
    bot_message = {
        **_TEAMS_MESSAGE,
        "from": {"user": {"id": bot_id, "displayName": "ActivityBot"}},
    }
    notification = {"clientState": TEAMS_PROFILE, "resource": "teams/messages/msg-001"}

    with patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock(return_value="tok-123")), \
         patch("app.webhooks.receivers.teams._fetch_message", new=AsyncMock(return_value=bot_message)), \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest, \
         patch("app.webhooks.receivers.teams.settings") as mock_settings:
        mock_settings.BOT_SERVICE_PRINCIPAL_ID = bot_id
        await _process_notification(notification)

    mock_ingest.assert_not_called()


async def test_teams_missing_client_state_skips_all():
    """Notification missing clientState or resource must be ignored immediately."""
    from app.webhooks.receivers.teams import _process_notification

    with patch("app.webhooks.receivers.teams.acquire_delegated_token", new=AsyncMock()) as mock_token, \
         patch("app.webhooks.receivers.teams.ingest", new=AsyncMock()) as mock_ingest:
        await _process_notification({"resource": "teams/messages/msg-001"})  # no clientState
        await _process_notification({"clientState": PROFILE})               # no resource

    mock_token.assert_not_called()
    mock_ingest.assert_not_called()


# ── Jira ──────────────────────────────────────────────────────────────────────

async def test_jira_issue_updated_ingest():
    from app.webhooks.receivers.jira import _process

    body = {
        "webhookEvent": "jira:issue_updated",
        "user": {"accountId": "acc-1"},
        "issue": {
            "id": "10001",
            "fields": {
                "summary": "Bug in login",
                "project": {"key": "PROJ"},
                "updated": "2026-06-23T12:00:00.000+0000",
            },
        },
    }

    with patch("app.webhooks.receivers.jira._resolve_profile", new=AsyncMock(return_value=PROFILE)), \
         patch("app.webhooks.receivers.jira.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body, "jira:issue_updated")

    mock_ingest.assert_called_once()
    event = mock_ingest.call_args[0][0]
    assert event["source"] == "jira"
    assert event["event_type"] == "jira:issue_updated"
    assert event["profile_id"] == PROFILE


async def test_jira_unresolved_profile_skips_ingest():
    from app.webhooks.receivers.jira import _process

    body = {"webhookEvent": "jira:issue_created", "user": {"accountId": "unknown"}}

    with patch("app.webhooks.receivers.jira._resolve_profile", new=AsyncMock(return_value=None)), \
         patch("app.webhooks.receivers.jira.ingest", new=AsyncMock()) as mock_ingest:
        await _process(body, "jira:issue_created")

    mock_ingest.assert_not_called()
