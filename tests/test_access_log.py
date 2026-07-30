"""The audit-log viewer endpoint: role/action filters reach the query, and
profile ids are resolved to emails (with a short-id fallback for unknown/deleted)."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.rbac import load_profile
from app.storage.postgres import get_db

ADMIN_ID = "00000000-0000-0000-0000-0000000000aa"
TID = "22222222-2222-2222-2222-222222222222"


def _cursor(docs):
    cur = MagicMock()
    cur.sort.return_value = cur
    cur.limit.return_value = cur
    cur.to_list = AsyncMock(return_value=docs)
    col = MagicMock()
    col.find.return_value = cur
    return col


async def _call(path, docs, resolved):
    from app.routes.user_management import router
    admin = SimpleNamespace(id=uuid.UUID(ADMIN_ID), email="admin@x.com", role="admin")
    db = AsyncMock()
    res = MagicMock()
    res.all = MagicMock(return_value=resolved)
    db.execute = AsyncMock(return_value=res)

    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[load_profile] = lambda: admin
    app.include_router(router)
    col = _cursor(docs)
    with patch("app.routes.user_management.access_log", return_value=col):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(path)
    return r, col


async def test_filters_reach_query_and_ids_resolve():
    docs = [
        {"actor_email": "admin@x.com", "actor_role": "admin", "action": "preview",
         "kind": "my_day", "target_profile_id": TID,
         "at": datetime(2026, 7, 24, tzinfo=timezone.utc)},
        {"actor_profile_id": "33333333-3333-3333-3333-333333333333", "actor_role": "supervisor",
         "action": "email_delivery", "kind": "analytics", "report_owner_id": TID,
         "recipient_email": "dev@x.com",
         "at": datetime(2026, 7, 24, tzinfo=timezone.utc)},
    ]
    r, col = await _call("/api/user-management/access-log?role=supervisor&action=email_delivery",
                         docs, [(uuid.UUID(TID), "target@x.com")])

    assert r.status_code == 200
    assert col.find.call_args[0][0] == {"actor_role": "supervisor", "action": "email_delivery"}

    ents = r.json()["entries"]
    assert ents[0]["target_email"] == "target@x.com"      # resolved from id
    assert ents[1]["recipient_email"] == "dev@x.com"       # stored email used
    assert ents[1]["actor_email"].startswith("33333333")   # unknown id → short fallback


async def test_no_filter_sends_empty_query():
    r, col = await _call("/api/user-management/access-log", [], [])
    assert r.status_code == 200
    assert col.find.call_args[0][0] == {}
    assert r.json()["entries"] == []
