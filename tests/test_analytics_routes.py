"""Endpoint tests for the My Activity /today route — the biggest change in the
timezone migration and previously untested end to end. Motor cursor chains are
mocked, so no infrastructure is required (mirrors tests/test_stats_routes.py)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routes.agent.analytics import router

PROFILE = "00000000-0000-0000-0000-000000000009"
P = "app.services.device_analytics."


def _coll(items=(), find_one=None):
    """Mock a Motor collection supporting find().sort().limit().to_list(),
    find_one(), and count_documents()."""
    cur = MagicMock()
    cur.sort.return_value = cur
    cur.limit.return_value = cur
    cur.to_list = AsyncMock(return_value=list(items))
    coll = MagicMock()
    coll.find.return_value = cur
    coll.find_one = AsyncMock(return_value=find_one)
    coll.count_documents = AsyncMock(return_value=len(list(items)))
    return coll


def _app():
    app = FastAPI()
    app.include_router(router)
    return app


async def _get(app, path, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path, **kw)


def _patches(hbs=(), last_hb=None, claude=(), ai=(), commits=()):
    return [
        patch("app.auth.sso.get_profile_from_session", new=AsyncMock(return_value=PROFILE)),
        patch(P + "device_heartbeats", return_value=_coll(hbs, find_one=last_hb)),
        patch(P + "ai_tool_events", return_value=_coll(ai)),
        patch(P + "claude_usage", return_value=_coll(claude)),
        patch(P + "local_commits", return_value=_coll(commits)),
        patch(P + "week_summaries", return_value=_coll()),
    ]


async def test_today_unauthenticated_returns_401():
    app = _app()
    with patch("app.auth.sso.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/today")
    assert r.status_code == 401


async def test_today_empty_returns_zeroed_shape():
    app = _app()
    ps = _patches()
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        r = await _get(app, "/today", params={"tz": "Asia/Kolkata"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_focus_min"] == 0
    assert body["focus_blocks"] == []
    assert body["active_now"] is None
    for key in ("active_tools", "tool_active_min", "claude_usage", "commits"):
        assert key in body


async def test_today_focus_computed_from_heartbeats():
    t0 = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
    hbs = [{"timestamp": t0 + timedelta(seconds=s)} for s in range(0, 20 * 60, 30)]
    app = _app()
    ps = _patches(hbs=hbs)
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        r = await _get(app, "/today", params={"tz": "Asia/Kolkata"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_focus_min"] == 19
    assert len(body["focus_blocks"]) == 1
