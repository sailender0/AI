"""One-off: correct calendar rows stored as local wall-clock instead of UTC.

The calendar connector sends `Prefer: outlook.timezone`, so Graph answers with
local times carrying NO offset ("2026-07-29T07:30:00.0000000"). Those parsed to
naive datetimes and were stored as-is, while calendar_activity._local() reads
every timestamp as UTC — so meetings rendered a whole offset out (a 07:30 PDT
standup at 00:30) and ones near midnight landed on the wrong day.

app/backfill/outlook_calendar.py now converts before storing, but rows written
before that keep the bad value: ingest() dedups on source_event_id, so re-polling
will NOT overwrite them. This shifts them once.

    docker compose exec app python scripts/fix_calendar_utc.py

RUN IT ONCE. A second run would shift the same rows again — it cannot tell an
already-corrected row from an uncorrected one. --dry-run prints without writing.
"""
import asyncio
import os
import sys
from datetime import timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select

from app.storage.models import Profile
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal


async def main(dry_run: bool) -> None:
    async with AsyncSessionLocal() as db:
        tz_by_profile = {
            str(p.id): (p.timezone or "UTC")
            for p in (await db.execute(select(Profile))).scalars().all()
        }

    changed = skipped = 0
    async for doc in activity_events().find({"source": "outlook_calendar"}):
        ts = doc.get("occurred_at")
        if ts is None:
            continue
        tz = ZoneInfo(tz_by_profile.get(str(doc.get("profile_id")), "UTC"))
        corrected = ts.replace(tzinfo=tz).astimezone(timezone.utc)
        if corrected.replace(tzinfo=None) == ts:
            skipped += 1
            continue
        print(f"{doc.get('title', '')[:40]:42} {ts} -> {corrected}")
        if not dry_run:
            await activity_events().update_one(
                {"_id": doc["_id"]}, {"$set": {"occurred_at": corrected}})
        changed += 1

    verb = "would change" if dry_run else "changed"
    print(f"\n{verb}: {changed}   already UTC: {skipped}")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv))
