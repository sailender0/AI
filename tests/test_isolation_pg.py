"""Tenant isolation against a REAL Postgres (integration — the b-spike).

Skips when the DB isn't reachable (see pg_session). Proves the harness works AND
that the chat-conversation ownership guard blocks cross-tenant reads: user A must
not be able to read user B's conversation.

Seeds its own rows and deletes them in `finally`. A full suite should run against
a dedicated *_test database, not dev.

The route is called directly rather than over HTTP, so FastAPI never resolves
`Depends(require_profile)` and `profile_id` is just an argument — no session, no
Redis, no mocking. An earlier version patched a `get_profile_from_session` symbol
that does not exist on this module (it lives in app.auth.sso) and passed a
`request=` the route has never taken; both raised, and nothing noticed because
this file only runs when a Postgres is reachable. Authentication itself is
covered by tests/test_auth_required.py — what this asserts is the ownership
predicate in the WHERE clause, which is the actual guard.
"""
from uuid import uuid4

from sqlalchemy import delete as sa_delete

from app.storage.models import ChatConversation, ChatMessage, Profile


async def test_user_cannot_read_another_users_conversation(pg_session):
    Session = pg_session

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
        from app.ai.chat import get_conversation_messages

        cross = await get_conversation_messages(conv_id=conv_id, profile_id=a_id)
        assert cross.status_code == 404, "cross-tenant read must be 404"
        assert b"secret" not in cross.body, "404 must not leak the message body"

        own = await get_conversation_messages(conv_id=conv_id, profile_id=b_id)
        assert own.status_code == 200, "owner must be able to read their conversation"
        assert b"secret" in own.body, "owner must actually get their message back"
    finally:
        async with Session() as db:
            await db.execute(sa_delete(ChatMessage).where(ChatMessage.conversation_id == conv_id))
            await db.execute(sa_delete(ChatConversation).where(ChatConversation.id == conv_id))
            await db.execute(sa_delete(Profile).where(Profile.id.in_([a_id, b_id])))
            await db.commit()
