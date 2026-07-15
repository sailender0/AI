"""
Stats route tests: auth guards and today/week branch response shapes.

All MongoDB/service calls are mocked so no infrastructure is required.
The four connector routes (github, jira, teams, gitlab) each have:
  - an auth guard test (unauthenticated → 401)
  - a period=week test (includes WoW change percentages)
  - a period=today test (no change field, no week_bounds call)
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.storage.postgres import get_db

PROFILE_ID = "00000000-0000-0000-0000-000000000002"

_NOW = datetime(2026, 6, 23, 10, 0, 0, tzinfo=timezone.utc)
_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAILY = [1, 2, 3, 4, 5, 6, 7]
_TOP = [{"key": "org/repo", "count": 5}]


def _mock_db():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result)
    return db


def _mini_stats_app():
    from app.routes.stats import router
    app = FastAPI()
    app.dependency_overrides[get_db] = _mock_db
    app.include_router(router)
    return app


async def _get(app, path, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path, **kwargs)


# Patch context for an authenticated, fully-mocked stats request.
def _auth_patches(module: str):
    return [
        patch(f"app.routes.stats.get_profile_from_session", new=AsyncMock(return_value=PROFILE_ID)),
        patch(f"app.routes.stats.get_profile_tz", new=AsyncMock(return_value="UTC")),
        patch(f"app.routes.stats.count", new=AsyncMock(return_value=10)),
        patch(f"app.routes.stats.daily_counts", new=AsyncMock(return_value=(_LABELS, _DAILY))),
        patch(f"app.routes.stats.top_items", new=AsyncMock(return_value=_TOP)),
        patch(f"app.routes.stats.workspace_breakdown", new=AsyncMock(return_value=_TOP)),
        patch(f"app.routes.stats.week_bounds", return_value=(_NOW, _NOW)),
    ]


# ── Auth guards ───────────────────────────────────────────────────────────────

async def test_github_stats_unauthenticated_returns_401():
    app = _mini_stats_app()
    with patch("app.routes.stats.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/github/stats")
    assert r.status_code == 401


async def test_jira_stats_unauthenticated_returns_401():
    app = _mini_stats_app()
    with patch("app.routes.stats.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/jira/stats")
    assert r.status_code == 401


async def test_teams_stats_unauthenticated_returns_401():
    app = _mini_stats_app()
    with patch("app.routes.stats.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/teams/stats")
    assert r.status_code == 401


async def test_gitlab_stats_unauthenticated_returns_401():
    app = _mini_stats_app()
    with patch("app.routes.stats.get_profile_from_session", new=AsyncMock(return_value=None)):
        r = await _get(app, "/api/gitlab/stats")
    assert r.status_code == 401


# ── GitHub stats ──────────────────────────────────────────────────────────────

async def test_github_stats_week_response_shape():
    app = _mini_stats_app()
    patches = _auth_patches("github")
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        r = await _get(app, "/api/github/stats", params={"period": "week"})
    assert r.status_code == 200
    body = r.json()
    assert "metrics" in body and "chart" in body and "top_items" in body
    assert any("change" in m for m in body["metrics"])  # WoW % present in week mode
    assert set(body["chart"]["datasets"]) >= {"commits", "pull_requests"}


async def test_github_stats_today_response_shape():
    app = _mini_stats_app()
    patches = _auth_patches("github")
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        r = await _get(app, "/api/github/stats", params={"period": "today"})
    assert r.status_code == 200
    body = r.json()
    assert "metrics" in body
    assert not any("change" in m for m in body["metrics"])  # no WoW % in today mode


# ── Jira stats ────────────────────────────────────────────────────────────────

async def test_jira_stats_response_shape():
    """Chart + top projects only — the KPI metrics block was removed with the
    page redesign (work KPIs come live from /api/jira/assigned instead)."""
    app = _mini_stats_app()
    patches = _auth_patches("jira")
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        r = await _get(app, "/api/jira/stats")
    assert r.status_code == 200
    body = r.json()
    assert "metrics" not in body
    assert set(body["chart"]["datasets"]) == {"created", "updated", "comments"}
    assert "top_items" in body and body["top_label"] == "Top Projects"


# ── Teams stats ───────────────────────────────────────────────────────────────

async def test_teams_stats_week_response_shape():
    app = _mini_stats_app()
    patches = _auth_patches("teams")
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        r = await _get(app, "/api/teams/stats", params={"period": "week"})
    assert r.status_code == 200
    body = r.json()
    assert "metrics" in body and "chart" in body
    assert any(m["label"] == "Messages" for m in body["metrics"])
    assert "messages" in body["chart"]["datasets"]


async def test_teams_stats_today_no_change_field():
    app = _mini_stats_app()
    patches = _auth_patches("teams")
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        r = await _get(app, "/api/teams/stats", params={"period": "today"})
    assert r.status_code == 200
    assert not any("change" in m for m in r.json()["metrics"])


# ── GitLab stats ──────────────────────────────────────────────────────────────

async def test_gitlab_stats_week_response_shape():
    app = _mini_stats_app()
    patches = _auth_patches("gitlab")
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        r = await _get(app, "/api/gitlab/stats", params={"period": "week"})
    assert r.status_code == 200
    body = r.json()
    assert "metrics" in body and "chart" in body
    labels = {m["label"] for m in body["metrics"]}
    assert {"Commits", "Merge Requests", "Pipelines"} <= labels


async def test_gitlab_stats_today_no_change_field():
    app = _mini_stats_app()
    patches = _auth_patches("gitlab")
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        r = await _get(app, "/api/gitlab/stats", params={"period": "today"})
    assert r.status_code == 200
    assert not any("change" in m for m in r.json()["metrics"])
