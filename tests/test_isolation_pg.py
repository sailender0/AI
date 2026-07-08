"""Tenant isolation against a REAL Postgres (integration — the b-spike).

Skips when the DB isn't reachable (see pg_session). Proves the harness works AND
that the chat-conversation ownership guard blocks cross-tenant reads: user A must
not be able to read user B's conversation.

Seeds its own rows and deletes them in `finally`. A full suite should run against
a dedicated *_test database, not dev.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import delete as sa_delete

from app.storage.models import ChatConversation, ChatMessage, Profile


async def test_user_cannot_read_another_users_conversation(pg_session):
    Session = pg_session

    # ── seed: profile A, profile B, and a conversation owned by B ──
    async with Session() as db:
        pa = Profile(entra_id=f"e{uuid4()}", email=f"a{uuid4()}@t")
        pb = Profile(entra_id=f"e{uuid4()}", email=f"b{uuid4()}@t")
        db.add_all([pa, pb])
        await db.flush()
        conv = ChatConversation(profile_id=pb.id, title="B's private chat")
        db.add(conv)
        await db.flush()
        db.add(ChatMessage(conversation_id=conv.id, role="user", content="secret"))
        await db.commit()
        a_id, b_id, conv_id = str(pa.id), str(pb.id), str(conv.id)

    try:
        from app.ai.query import get_conversation_messages

        # A tries to read B's conversation → 404 (isolation holds)
        with patch("app.ai.query.get_profile_from_session", new=AsyncMock(return_value=a_id)):
            cross = await get_conversation_messages(request=None, conv_id=conv_id)
        assert cross.status_code == 404, "cross-tenant read must be 404"

        # B reads its own → 200 (positive control: the row really exists)
        with patch("app.ai.query.get_profile_from_session", new=AsyncMock(return_value=b_id)):
            own = await get_conversation_messages(request=None, conv_id=conv_id)
        assert own.status_code == 200, "owner must be able to read their conversation"
    finally:
        async with Session() as db:
            await db.execute(sa_delete(ChatMessage).where(ChatMessage.conversation_id == conv_id))
            await db.execute(sa_delete(ChatConversation).where(ChatConversation.id == conv_id))
            await db.execute(sa_delete(Profile).where(Profile.id.in_([a_id, b_id])))
            await db.commit()
