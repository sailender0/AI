"""The tool-definitions endpoint must return a JSON *object*, not a double-encoded
string. If it double-encodes, the agent's r.json() yields a str, r.json().get(...)
raises, and remote tool maps silently never apply. No infra needed — the handler
just reads a file (same ASGITransport pattern as test_auth_required.py).
"""
from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_tool_definitions_returns_object_not_string():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/agent/tool-definitions")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict), "endpoint double-encoded the body into a JSON string"
    assert "ollama" in data.get("proc_map", {})
