"""
Normalizer and dedup.

sanitize() strips prompt-injection patterns before storing user-generated text.
duplicate() uses a Redis fast-path before falling back to MongoDB.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo.errors import DuplicateKeyError

from app.storage.mongodb import activity_events
from app.storage.redis_client import get_redis

_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+previous|system\s*:|<\s*/?system\s*>|assistant\s*:|<\s*/?assistant\s*>)",
    re.IGNORECASE,
)


def sanitize(text: str) -> str:
    cleaned = _INJECTION_PATTERNS.sub("", text or "")
    return cleaned.strip()[:500]


def _parse_ts(raw: dict, source: str) -> datetime:
    candidates = {
        "teams_subscription": lambda r: r.get("createdDateTime") or r.get("lastModifiedDateTime"),
        "github": lambda r: r.get("created_at") or r.get("updated_at"),
        "gitlab": lambda r: (r.get("_commit") or {}).get("timestamp")
                            or r.get("created_at") or r.get("updated_at"),
        "jira":   lambda r: (r.get("issue", {}) or {}).get("fields", {}).get("updated")
                            or r.get("timestamp"),
    }
    raw_ts = candidates.get(source, lambda r: None)(raw)
    if raw_ts:
        try:
            return datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _extract_title(source: str, raw: dict) -> str:
    if source == "teams_subscription":
        # The correspondent, never the message content. `title` is indexed, shown
        # in the timeline, exported to CSV/PDF and fed to the AI summariser, so a
        # body here would archive message text across the whole app. Paired with
        # the $select in receivers/teams.py, which keeps `body` out of the
        # response in the first place.
        return ((raw.get("from") or {}).get("emailAddress") or {}).get("address", "")
    if source == "github":
        return (
            raw.get("pull_request", {}).get("title")
            or raw.get("issue", {}).get("title")
            or raw.get("head_commit", {}).get("message", "")
        )
    if source == "gitlab":
        # Push events carry a per-commit dict injected as "_commit"
        commit = raw.get("_commit")
        if commit:
            return commit.get("message", "").split("\n")[0]
        return (
            raw.get("object_attributes", {}).get("title")
            or raw.get("object_attributes", {}).get("description", "")
        )
    if source == "jira":
        return (raw.get("issue", {}) or {}).get("fields", {}).get("summary", "")
    return ""


def _map_event_type(source: str, raw: dict, event_type: str | None) -> str:
    if event_type:
        return event_type
    if source == "github":
        if "pull_request" in raw:
            action = raw.get("action", "")
            return "pr_merged" if action == "closed" and raw["pull_request"].get("merged") else f"pr_{action}"
        if "commits" in raw:
            return "commit"
        if "issue" in raw:
            return "issue_updated"
    if source == "gitlab":
        return raw.get("object_kind", "unknown")
    if source == "jira":
        return raw.get("webhookEvent", "unknown")
    if source == "teams_subscription":
        return "message_sent"
    return "unknown"


def _extract_native_id(source: str, raw: dict) -> str:
    if source == "teams_subscription":
        return raw.get("id", "")
    if source == "github":
        return str(
            raw.get("pull_request", {}).get("id")
            or raw.get("issue", {}).get("id")
            or raw.get("after", "")
        )
    if source == "gitlab":
        commit = raw.get("_commit")
        if commit:
            return commit.get("id", "")
        return str(raw.get("object_attributes", {}).get("id", ""))
    if source == "jira":
        return (raw.get("issue", {}) or {}).get("id", "")
    return ""


def _extract_workspace(source: str, raw: dict) -> str | None:
    if source == "github":
        repo = raw.get("repository") or {}
        return repo.get("full_name") or repo.get("name")
    if source == "gitlab":
        project = raw.get("project") or {}
        return project.get("path_with_namespace") or project.get("name")
    if source == "jira":
        fields = (raw.get("issue") or {}).get("fields") or {}
        project = fields.get("project") or {}
        return project.get("key") or project.get("name")
    return None


def _extract_due_date(source: str, raw: dict) -> datetime | None:
    if source == "jira":
        due = (raw.get("issue", {}) or {}).get("fields", {}).get("duedate")
        if due:
            try:
                return datetime.fromisoformat(due)
            except ValueError:
                pass
    return None


def normalize(raw: dict, source: str, profile_id: str, event_type: str | None = None) -> dict:
    return {
        "_id": str(uuid.uuid4()),
        "profile_id": profile_id,
        "source": source,
        "event_type": _map_event_type(source, raw, event_type),
        "occurred_at": _parse_ts(raw, source),
        "title": sanitize(_extract_title(source, raw)),
        "due_date": _extract_due_date(source, raw),
        "workspace": _extract_workspace(source, raw),
        "raw_payload": raw,
        "source_event_id": _extract_native_id(source, raw),
    }


def _dedup_key(event: dict) -> str:
    return f"dedup:{event['profile_id']}:{event['source']}:{event['source_event_id']}:{event['event_type']}"


async def is_duplicate(event: dict) -> bool:
    key = _dedup_key(event)
    redis = get_redis()
    if await redis.exists(key):
        return True
    exists = await activity_events().find_one(
        {
            "profile_id": event["profile_id"],
            "source": event["source"],
            "source_event_id": event["source_event_id"],
            "event_type": event["event_type"],
        }
    )
    if exists:
        await redis.set(key, "1", ex=86400)
        return True
    return False


async def ingest(event: dict) -> bool:
    """Store a normalized event unless it's a duplicate. Returns True if newly
    inserted, False if deduped — the backfill runner uses this for its counts.
    The unique index backstops the is_duplicate() check against races
    (concurrent webhook + backfill inserting the same event)."""
    if await is_duplicate(event):
        return False
    try:
        await activity_events().insert_one(event)
    except DuplicateKeyError:
        return False
    await get_redis().set(_dedup_key(event), "1", ex=86400)
    import asyncio
    from app.ws_manager import manager as _ws
    asyncio.create_task(_ws.notify(
        str(event.get("profile_id", "")),
        {
            "type":       "new_event",
            "source":     event.get("source", ""),
            "event_type": event.get("event_type", ""),
        },
    ))
    return True
