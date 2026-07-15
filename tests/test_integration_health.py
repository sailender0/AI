"""Connector health against a REAL Postgres (see pg_session — skips when the DB
is unreachable, FAILS in CI via REQUIRE_DB).

Regression for the July 2026 silent-death incident: a Jira token expired with no
refresh token, get_valid_token kept returning it, and the UI showed a green dot
for 18 days. An unrefreshable expired token must yield None AND flip sync_status
to 'error' so /api/me surfaces the breakage (amber dot + reconnect banner).
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete as sa_delete, select

from app.auth.oauth import get_valid_token, mark_integration_error
from app.storage.models import Integration, JiraIntegration, Profile


async def test_expired_token_without_refresh_marks_error(pg_session):
    Session = pg_session
    async with Session() as db:
        p = Profile(entra_id=f"e{uuid4()}", email=f"health{uuid4()}@t")
        db.add(p)
        await db.flush()
        row = JiraIntegration(
            profile_id=p.id,
            access_token_enc="enc-doesnt-matter",   # never decrypted: None returned first
            refresh_token_enc=None,
            token_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            sync_status="active",
        )
        db.add(row)
        await db.commit()
        pid, iid = str(p.id), str(row.id)

    async def status():
        async with Session() as db:
            return (await db.execute(
                select(Integration.sync_status).where(Integration.id == iid)
            )).scalar_one()

    try:
        assert await get_valid_token(pid, "jira") is None
        assert await status() == "error", "dead token must be surfaced, not silent"

        # the live-API probe path: a provider 401 flags the row the same way
        async with Session() as db:
            (await db.get(Integration, iid)).sync_status = "active"
            await db.commit()
        await mark_integration_error(pid, "jira")
        assert await status() == "error"
    finally:
        async with Session() as db:
            await db.execute(sa_delete(Integration).where(Integration.id == iid))
            await db.execute(sa_delete(Profile).where(Profile.id == pid))
            await db.commit()
