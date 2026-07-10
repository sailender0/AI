"""Backfill runner: token -> fetch -> ingest (dedup). See docs/adr-0003-backfill.md.

The per-source fetch is injectable so the orchestration (token gate, ingest,
dedup counting) is testable without hitting a live API.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.auth.oauth import get_valid_token
from app.backfill import github, gitlab, jira
from app.webhooks.normalizer import ingest

logger = logging.getLogger(__name__)

_FETCHERS = {
    "github": github.fetch_events,
    "gitlab": gitlab.fetch_events,
    "jira":   jira.fetch_events,
}
SUPPORTED = frozenset(_FETCHERS)
_MAX_DAYS = 90


async def run_backfill(profile_id: str, source: str, days: int = 30, *, fetch=None) -> dict:
    """Fetch recent history for one connector and ingest it. Idempotent: ingest()
    dedups every event against existing rows via the unique index, so re-running
    is safe. Returns a summary dict (also logged; discarded when run as a task)."""
    days = max(1, min(days, _MAX_DAYS))
    fetch = fetch or _FETCHERS.get(source)
    if fetch is None:
        return {"error": "unsupported_source", "source": source}

    token = await get_valid_token(profile_id, source)
    if not token:
        return {"error": "no_token", "source": source}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = await fetch(token, profile_id, since)

    inserted = 0
    for ev in events:
        if await ingest(ev):
            inserted += 1

    result = {"source": source, "days": days, "fetched": len(events),
              "inserted": inserted, "deduped": len(events) - inserted}
    logger.info("Backfill %s profile=%s: %s", source, profile_id[:8], result)
    return result
