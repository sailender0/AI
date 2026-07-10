"""Event backfill — reconstruct activity from connector REST APIs.

Only the pure REST->event mappers live here for now (github.py, gitlab.py).
They produce the SAME normalized shape and (source_event_id, event_type) as the
live webhook path, so app.webhooks.normalizer.ingest() deduplicates a backfilled
row against its webhook twin via the unique index. See docs/adr-0003-backfill.md.

The fetch/runner/route layer talks to live GitHub/GitLab APIs and needs real
tokens to verify, so it is deferred until creds are available (ADR Phase-1
remainder). These mappers are the correctness-critical, offline-testable core.
"""
import uuid
from datetime import datetime, timezone


def parse_iso(raw_ts) -> datetime:
    """The same lenient ISO parse the normalizer uses; falls back to now()."""
    if raw_ts:
        try:
            return datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def make_event(*, profile_id: str, source: str, event_type: str,
               source_event_id: str, title: str, occurred_at: datetime,
               workspace: str | None = None, due_date: datetime | None = None,
               raw: dict | None = None) -> dict:
    """Build an event dict identical in shape to normalizer.normalize()'s output,
    so ingest() accepts it and the unique dedup index applies. The dedup key is
    (profile_id, source, source_event_id, event_type)."""
    return {
        "_id": str(uuid.uuid4()),
        "profile_id": profile_id,
        "source": source,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "title": title,
        "due_date": due_date,
        "workspace": workspace,
        "raw_payload": raw or {},
        "source_event_id": source_event_id,
    }
