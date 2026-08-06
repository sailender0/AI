"""One-off: rewrite stored call rows with each person's own join/leave times.

Call rows used to take occurred_at and minutes from the call record itself — when
the CALL started and how long IT ran. For anyone who joined late or left early
that is somebody else's timing: a call you were pulled into at 09:16 read as
starting 08:25 and lasting 102 minutes instead of 51.

app/backfill/teams_calls.py now reads `sessions($expand=segments)` for the real
per-person times, but rows written before that keep the old values: ingest()
dedups on source_event_id, so re-polling will NOT overwrite them.

    docker compose exec app python scripts/fix_call_times.py [--dry-run]

Safe to re-run: times are re-derived from Graph each time, not shifted. Records
older than the ~30 day retention window are gone from Graph and are left as they
are, with a warning.
"""
import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select

from app.auth.sso import acquire_app_token
from app.backfill.teams_calls import fetch_sessions, presence
from app.storage.models import Profile
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal


async def main(dry_run: bool) -> None:
    token = await acquire_app_token()
    if not token:
        print("no app-only token — check AZURE_CLIENT_SECRET")
        return

    async with AsyncSessionLocal() as db:
        oid_by_profile = {
            str(p.id): p.teams_user_id
            for p in (await db.execute(select(Profile))).scalars().all()
            if p.teams_user_id
        }

    headers = {"Authorization": f"Bearer {token}"}
    changed = gone = unchanged = 0

    async with httpx.AsyncClient(timeout=60) as client:
        async for doc in activity_events().find({"source": "teams_call"}):
            oid = oid_by_profile.get(str(doc.get("profile_id")))
            record_id = doc.get("source_event_id")
            if not oid or not record_id:
                continue

            sessions = await fetch_sessions(client, headers, record_id)
            if not sessions:
                print(f"  no sessions for {doc.get('title', '')[:32]} ({doc['occurred_at']})")
                gone += 1
                continue

            joined, left = presence(sessions, oid)
            if joined is None:
                gone += 1
                continue
            minutes = max(int((left - joined).total_seconds() // 60), 0) if left else 0
            drift = abs((doc["occurred_at"] - joined.replace(tzinfo=None)).total_seconds())
            if drift < 1 and doc["raw_payload"].get("minutes") == minutes:
                unchanged += 1
                continue

            print(f"  {doc.get('title', '')[:32]:34} {doc['occurred_at']} "
                  f"({doc['raw_payload'].get('minutes')}m) -> {joined} ({minutes}m)")
            if not dry_run:
                await activity_events().update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"occurred_at": joined,
                              "raw_payload.minutes": minutes,
                              "raw_payload.own_times": True}})
            changed += 1

    verb = "would change" if dry_run else "changed"
    print(f"\n{verb}: {changed}   already correct: {unchanged}   "
          f"no sessions available: {gone}")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv))
