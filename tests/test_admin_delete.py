"""Deleting a user: the Mongo purge and the self-delete guard.

Postgres cleanup is the ORM cascade (declared on Profile), so it isn't re-tested
here — what needs covering is the half Mongo has no foreign keys for, and the
guard that stops an admin removing their own account.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import load_profile
from app.storage.mongodb import purge_profile
from app.storage.postgres import get_db

ADMIN_ID = "00000000-0000-0000-0000-0000000000aa"


async def test_purge_profile_clears_activity_but_keeps_audit():
    """Every per-profile collection is scoped by profile_id — and access_log,
    the audit trail, is left alone."""
    seen = {}

    class FakeCol:
        def __init__(self, name):
            self.name = name

        async def delete_many(self, query):
            seen[self.name] = query
            return SimpleNamespace(deleted_count=2)

    class FakeDB:
        def __getitem__(self, name):
            return FakeCol(name)

    with patch("app.storage.mongodb.get_db", return_value=FakeDB()):
        out = await purge_profile("p1")

    assert "access_log" not in seen, "audit trail must survive user deletion"
    assert seen["activity_events"] == {"profile_id": "p1"}
    assert len(seen) == 10
    assert out["activity_events"] == 2


async def test_cannot_delete_self():
    from app.routes.user_management import router

    admin = SimpleNamespace(id=uuid.UUID(ADMIN_ID), email="admin@x.com", role="admin")
    db = AsyncMock(spec=AsyncSession)
    db.get = AsyncMock(return_value=None)

    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[load_profile] = lambda: admin
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.delete(f"/api/user-management/users/{ADMIN_ID}")

    assert r.status_code == 400
    assert "yourself" in r.json()["error"]
    db.delete.assert_not_called()
