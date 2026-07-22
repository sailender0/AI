"""Real-Postgres test for the GitHub webhook actor→profile resolver.

Proves _resolve_profile matches an incoming sender.id to the LinkedIdentity row
that owns it, and returns None for an actor nobody connected (the drop guard that
keeps a stranger's events out). The receiver-level tests mock this lookup; this
one exercises the actual DB query. Skips when Postgres isn't reachable (pg_session).
"""
from uuid import uuid4

from sqlalchemy import delete as sa_delete

from app.storage.models import LinkedIdentity, Profile


async def test_resolve_profile_matches_actor_and_drops_unknown(pg_session):
    Session = pg_session
    gh_id = f"gh-{uuid4().hex[:8]}"

    # ── seed: a profile that has connected GitHub as actor `gh_id` ──
    async with Session() as db:
        prof = Profile(entra_id=f"e{uuid4()}", email=f"u{uuid4()}@t")
        db.add(prof)
        await db.flush()
        db.add(LinkedIdentity(
            profile_id=prof.id, provider="github",
            tenant_id=gh_id, workspace_label="octocat",
        ))
        await db.commit()
        profile_id = str(prof.id)

    try:
        from app.webhooks.receivers.github import _resolve_profile

        # known actor → its owning profile
        assert await _resolve_profile(gh_id) == profile_id
        # actor nobody connected → None (the event would be dropped)
        assert await _resolve_profile(f"nobody-{uuid4().hex[:8]}") is None
    finally:
        async with Session() as db:
            await db.execute(sa_delete(LinkedIdentity).where(LinkedIdentity.tenant_id == gh_id))
            await db.execute(sa_delete(Profile).where(Profile.id == profile_id))
            await db.commit()
