"""Auth contract: protected routes reject unauthenticated callers with 401.

Runs the real ASGI app via httpx ASGITransport (no lifespan → no scheduler/DB
init). A request with no session cookie / no Bearer token short-circuits to 401
before the handler touches Redis or Postgres, so this needs no infrastructure.
Bodies are valid where a schema is required, so the ONLY failure is auth (401),
never validation (422). Guards a route someone forgets to protect.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# (method, path, body) — require a signed-in session cookie
SESSION_ROUTES = [
    ("GET",   "/api/github/stats",       None),
    ("GET",   "/api/jira/stats",         None),
    ("GET",   "/api/gitlab/stats",       None),
    ("GET",   "/api/teams/stats",        None),
    ("GET",   "/api/agent/today",        None),
    ("GET",   "/api/agent/week",         None),
    ("GET",   "/api/agent/devices",      None),
    ("POST",  "/api/agent/register",     {"device_name": "t"}),
    ("GET",   "/api/standup/today",      None),
    ("GET",   "/api/standup/history",    None),
    ("GET",   "/api/export/daily-pdf",   None),
    ("GET",   "/api/export/weekly-csv",  None),
    ("GET",   "/api/chat/conversations", None),
    ("POST",  "/api/chat/conversations", None),
    ("PATCH", "/api/profile/timezone",   {}),
    ("GET",   "/api/email/preferences",  None),
    ("PUT",   "/api/email/preferences",  {"kind": "standup"}),
    ("POST",  "/api/email/preview",      {"kind": "standup"}),
    ("POST",  "/api/email/send",         {"kind": "standup"}),
]

# (method, path, body) — require a device Bearer token
DEVICE_ROUTES = [
    ("POST", "/api/agent/heartbeat",         {}),
    ("POST", "/api/agent/commit",            {"repo": "r", "branch": "b", "sha": "s", "message": "m"}),
    ("POST", "/api/agent/claude-usage",      {"entries": []}),
    ("POST", "/api/agent/ai-event",          {"tools": []}),
    ("POST", "/api/agent/vscode-extensions", {"extensions": []}),
    ("GET",  "/api/agent/status",            None),
    ("GET",  "/api/agent/standup/pending",   None),
    ("POST", "/api/agent/standup/ack",       {"date": "2026-07-07"}),
]

# public — must NOT require auth
PUBLIC_ROUTES = [
    ("GET", "/health"),
    ("GET", "/api/agent/tool-definitions"),
    ("GET", "/api/me"),
]


@pytest.mark.parametrize("method,path,body", SESSION_ROUTES + DEVICE_ROUTES,
                         ids=[f"{m} {p}" for m, p, _ in SESSION_ROUTES + DEVICE_ROUTES])
async def test_protected_routes_reject_unauthenticated(method, path, body):
    async with _client() as c:
        r = await c.request(method, path, json=body)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}, expected 401"


@pytest.mark.parametrize("method,path", PUBLIC_ROUTES, ids=[f"{m} {p}" for m, p in PUBLIC_ROUTES])
async def test_public_routes_do_not_require_auth(method, path):
    async with _client() as c:
        r = await c.request(method, path)
    assert r.status_code != 401, f"{method} {path} -> {r.status_code}, should be public"
