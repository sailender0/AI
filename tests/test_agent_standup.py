"""Agent-side standup delivery (ADR-0002 PR3): the pull/ack client methods and
the show-once decision. The toast itself is GUI (manual smoke)."""
from unittest.mock import MagicMock, patch

from agent.agent import AgentClient, _should_notify


def _client():
    return AgentClient("tok", "http://backend/")


def test_should_notify_new_standup():
    assert _should_notify({"date": "2026-07-01", "text": "x"}, None) is True
    assert _should_notify({"date": "2026-07-01", "text": "x"}, "2026-06-30") is True


def test_should_notify_skips_already_shown():
    assert _should_notify({"date": "2026-07-01"}, "2026-07-01") is False


def test_should_notify_skips_none():
    assert _should_notify(None, None) is False
    assert _should_notify(None, "2026-07-01") is False


def test_get_pending_standup_returns_content():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"standup": {"date": "2026-07-01", "text": "did stuff"}}
    with patch("agent.agent.requests.get", return_value=resp) as g:
        out = _client().get_pending_standup()
    assert out == {"date": "2026-07-01", "text": "did stuff"}
    assert g.call_args.args[0] == "http://backend/api/agent/standup/pending"
    assert g.call_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


def test_get_pending_standup_none_when_empty_or_error():
    empty = MagicMock(status_code=200)
    empty.json.return_value = {"standup": None}
    with patch("agent.agent.requests.get", return_value=empty):
        assert _client().get_pending_standup() is None
    with patch("agent.agent.requests.get", side_effect=Exception("boom")):
        assert _client().get_pending_standup() is None


def test_ack_standup_posts_date():
    resp = MagicMock(status_code=200)
    with patch("agent.agent.requests.post", return_value=resp) as p:
        assert _client().ack_standup("2026-07-01") is True
    assert p.call_args.args[0] == "http://backend/api/agent/standup/ack"
    assert p.call_args.kwargs["json"] == {"date": "2026-07-01"}
