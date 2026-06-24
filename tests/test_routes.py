"""
Route layer tests: auth guards, input validation, and response shape.

Uses a minimal FastAPI app (no scheduler/lifespan) with httpx AsyncClient.
get_db is overridden so no real PostgreSQL connection is made.
get_profile_from_session is patched per-module to simulate auth state.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.postgres import get_db

PROFILE_ID = "00000000-0000-0000-0000-000000000001"


# ── Test infrastructure ───────────────────────────────────────────────────────

def _mock_db() -> AsyncSession:
    """Minimal AsyncSession mock: execute returns no rows, get returns None."""
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    return db


def _mini_app(*routers) -> FastAPI:
    """Create a bare FastAPI app with the given routers and a mocked DB dependency."""
    app = FastAPI()
    app.dependency_overrides[get_db] = _mock_db
    for r in routers:
        app.include_router(r)
    return app


async def _get(app, path, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path, **kwargs)


async def _patch(app, path, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.patch(path, **kwargs)


# ── Auth guards ───────────────────────────────────────────────────────────────

async def test_get_me_unauthenticated_returns_authenticated_false():
    from app.routes.profile import router
    app = _mini_app(router)
    with patch("app.routes.profile.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/me")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


async def test_patch_timezone_unauthenticated_returns_401():
    from app.routes.profile import router
    app = _mini_app(router)
    with patch("app.routes.profile.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _patch(app, "/api/profile/timezone", json={"timezone": "UTC"})
    assert r.status_code == 401


async def test_get_stats_unauthenticated_returns_401():
    from app.routes.activity import router
    app = _mini_app(router)
    with patch("app.routes.activity.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/stats")
    assert r.status_code == 401


async def test_get_week_stats_unauthenticated_returns_401():
    from app.routes.activity import router
    app = _mini_app(router)
    with patch("app.routes.activity.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/week-stats", params={"start": "2026-06-16", "end": "2026-06-22"})
    assert r.status_code == 401


async def test_get_events_recent_unauthenticated_returns_401():
    from app.routes.activity import router
    app = _mini_app(router)
    with patch("app.routes.activity.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/events/recent")
    assert r.status_code == 401


# ── Input validation ──────────────────────────────────────────────────────────

async def test_patch_timezone_invalid_tz_returns_400():
    from app.routes.profile import router
    app = _mini_app(router)
    with patch("app.routes.profile.get_profile_from_session", new=AsyncMock(return_value=PROFILE_ID)):
        r = await _patch(app, "/api/profile/timezone", json={"timezone": "Not/AReal/Timezone"})
    assert r.status_code == 400
    assert "invalid timezone" in r.json()["error"]


async def test_get_week_stats_invalid_date_returns_400():
    from app.routes.activity import router
    app = _mini_app(router)
    with patch("app.routes.activity.get_profile_from_session", new=AsyncMock(return_value=PROFILE_ID)), \
         patch("app.routes.activity.get_profile_tz", new=AsyncMock(return_value="UTC")):
        r = await _get(app, "/api/week-stats", params={"start": "not-a-date", "end": "also-bad"})
    assert r.status_code == 400


async def test_get_day_data_invalid_date_returns_400():
    from app.routes.activity import router
    app = _mini_app(router)
    with patch("app.routes.activity.get_profile_from_session", new=AsyncMock(return_value=PROFILE_ID)), \
         patch("app.routes.activity.get_profile_tz", new=AsyncMock(return_value="UTC")):
        r = await _get(app, "/api/day-data", params={"date": "not-a-date"})
    assert r.status_code == 400


# ── Response shape ────────────────────────────────────────────────────────────

async def test_get_me_authenticated_returns_correct_shape():
    from app.routes.profile import router

    class _FakeProfile:
        id = uuid.UUID(PROFILE_ID)
        email = "dev@example.com"
        timezone = "UTC"

    db = _mock_db()
    db.get = AsyncMock(return_value=_FakeProfile())

    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(router)

    with patch("app.routes.profile.get_profile_from_session", new=AsyncMock(return_value=PROFILE_ID)), \
         patch("app.routes.profile.get_integrations", new=AsyncMock(return_value=(["github"], {"github": False}))):
        r = await _get(app, "/api/me")

    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["email"] == "dev@example.com"
    assert body["profile_id"] == PROFILE_ID
    assert "integrations" in body
    assert "integration_errors" in body
    assert "connect_urls" in body


async def test_patch_timezone_valid_returns_ok():
    from app.routes.profile import router

    class _FakeProfile:
        id = uuid.UUID(PROFILE_ID)
        timezone = "UTC"

    db = _mock_db()
    db.get = AsyncMock(return_value=_FakeProfile())

    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(router)

    with patch("app.routes.profile.get_profile_from_session", new=AsyncMock(return_value=PROFILE_ID)):
        r = await _patch(app, "/api/profile/timezone", json={"timezone": "Europe/London"})

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["timezone"] == "Europe/London"
