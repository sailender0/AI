"""The Device Activity gate follows the login, not the browser.

Regression: the gate used to be a `da_desktop` cookie set by the desktop login,
which only ever cookied the one browser it opened. Chrome showed the data; Brave
on the same machine, same profile, agent running, showed "Download & Install".
So the assertions below send NO cookies — a profile with a registered agent must
get the real page anyway.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routes import pages


@pytest.fixture
def gate(monkeypatch):
    """Stub the session + device lookup so this needs no Redis or Postgres."""
    def _set(*, logged_in: bool, has_agent: bool):
        async def _profile(_request):
            return "11111111-1111-1111-1111-111111111111" if logged_in else None

        async def _has_agent(_profile_id):
            return has_agent

        monkeypatch.setattr(pages, "get_profile_from_session", _profile)
        monkeypatch.setattr(pages, "_has_agent", _has_agent)
    return _set


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_agent_registered_shows_activity_without_any_cookie(gate):
    gate(logged_in=True, has_agent=True)
    async with _client() as c:
        r = await c.get("/my-activity")
    assert r.status_code == 200
    assert "Download &amp; Install" not in r.text
    assert "Desktop app required" not in r.text


async def test_no_agent_shows_download_screen(gate):
    gate(logged_in=True, has_agent=False)
    async with _client() as c:
        r = await c.get("/my-activity")
    assert r.status_code == 200
    assert "Desktop app required" in r.text


async def test_ai_tools_subpage_follows_the_same_gate(gate):
    gate(logged_in=True, has_agent=True)
    async with _client() as c:
        r = await c.get("/my-activity/ai-tools")
    assert r.status_code == 200

    gate(logged_in=True, has_agent=False)
    async with _client() as c:
        r = await c.get("/my-activity/ai-tools", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/my-activity"
