"""Endpoint tests for the agent-pull standup delivery (ADR-0002 PR2).
Device auth is overridden; the standups collection is mocked — no infra."""
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routes.agent._base import _get_device
from app.routes.agent.ingest import router

PROFILE = "00000000-0000-0000-0000-000000000007"
P = "app.routes.agent.ingest."


def _fake_device():
    d = MagicMock()
    d.id = "device-1"
    return (d, PROFILE)


def _app(authed=True):
    app = FastAPI()
    app.include_router(router)
    if authed:
        app.dependency_overrides[_get_device] = _fake_device
    return app


async def _req(app, method, path, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, **kw)


def _standups(find_one=None):
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value=find_one)
    coll.update_one = AsyncMock()
    return coll


async def test_pending_returns_flagged_standup():
    doc = {"date": "2026-07-01", "text": "did stuff", "delivery_pending": True}
    with patch(P + "standups", return_value=_standups(find_one=doc)):
        r = await _req(_app(), "GET", "/standup/pending")
    assert r.status_code == 200
    assert r.json()["standup"] == {"date": "2026-07-01", "text": "did stuff"}


async def test_pending_null_when_none():
    with patch(P + "standups", return_value=_standups(find_one=None)):
        r = await _req(_app(), "GET", "/standup/pending")
    assert r.status_code == 200
    assert r.json()["standup"] is None


async def test_ack_clears_flag():
    coll = _standups()
    with patch(P + "standups", return_value=coll):
        r = await _req(_app(), "POST", "/standup/ack", json={"date": "2026-07-01"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    coll.update_one.assert_awaited_once()
    filt, update = coll.update_one.call_args.args
    assert filt == {"profile_id": PROFILE, "date": "2026-07-01"}
    assert update == {"$set": {"delivery_pending": False}}


async def test_pending_unauthenticated_returns_401():
    r = await _req(_app(authed=False), "GET", "/standup/pending")
    assert r.status_code == 401
