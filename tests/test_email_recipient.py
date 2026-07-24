"""Recipient authorization for cross-user email sends (app/routes/email).
Default recipient is the actor; sending to anyone else is elevated-only + audited."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routes.email import _resolve_recipient

ACTOR_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


def _actor(role):
    return SimpleNamespace(id=uuid.UUID(ACTOR_ID), email="admin@x.com", role=role)


def _audit_ok():
    col = MagicMock()
    col.insert_one = AsyncMock()
    return patch("app.routes.email.access_log", return_value=col)


async def test_default_recipient_is_actor():
    actor = _actor("user")
    db = AsyncMock()
    assert await _resolve_recipient(None, actor, ACTOR_ID, "my_day", db) is actor
    assert await _resolve_recipient(ACTOR_ID, actor, ACTOR_ID, "my_day", db) is actor


async def test_regular_user_cannot_send_to_others():
    with pytest.raises(HTTPException) as e:
        await _resolve_recipient(OTHER_ID, _actor("user"), ACTOR_ID, "my_day", AsyncMock())
    assert e.value.status_code == 403


async def test_elevated_can_send_to_another_user_and_audits():
    recip = SimpleNamespace(id=uuid.UUID(OTHER_ID), email="dev@x.com")
    db = AsyncMock()
    db.get = AsyncMock(return_value=recip)
    with _audit_ok() as al:
        out = await _resolve_recipient(OTHER_ID, _actor("admin"), ACTOR_ID, "my_day", db)
    assert out is recip
    al.return_value.insert_one.assert_awaited_once()


async def test_bad_uuid_is_404():
    with pytest.raises(HTTPException) as e:
        await _resolve_recipient("not-a-uuid", _actor("admin"), ACTOR_ID, "my_day", AsyncMock())
    assert e.value.status_code == 404


async def test_missing_recipient_is_404():
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as e:
        await _resolve_recipient(OTHER_ID, _actor("admin"), ACTOR_ID, "my_day", db)
    assert e.value.status_code == 404
