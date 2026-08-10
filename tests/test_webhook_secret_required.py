"""Fail-closed webhook auth: an unset shared secret must REJECT, not accept.

compare_digest("", "") is True and an HMAC keyed on b"" is forgeable, so a blank
secret used to let anyone post spoofed events into a user's timeline. These guard
that regression — a receiver that drops its `not secret` check turns red here.

No infrastructure: the secret check short-circuits to 401 before the handler
touches the body, Redis, or Mongo.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture(autouse=True)
def _blank_secrets(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "GITLAB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "JIRA_WEBHOOK_SECRET", "")


async def test_github_blank_secret_rejects():
    async with _client() as c:
        r = await c.post("/webhook/github", json={"zen": "x"},
                         headers={"X-Hub-Signature-256": "sha256=" + "0" * 64,
                                  "X-GitHub-Event": "ping"})
    assert r.status_code == 401


async def test_gitlab_blank_secret_rejects():
    async with _client() as c:
        r = await c.post("/webhook/gitlab", json={"object_kind": "push"},
                         headers={"X-Gitlab-Token": ""})
    assert r.status_code == 401


async def test_jira_blank_secret_rejects():
    async with _client() as c:
        r = await c.post("/webhook/jira?secret=", json={"webhookEvent": "x"})
    assert r.status_code == 401
