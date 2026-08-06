"""GitLab REST item -> normalized event.

Each mapper reproduces the (source_event_id, event_type, title) the live webhook
receiver produces for the same object, so backfilled rows dedup against webhook
rows. See docs/adr-0003-backfill.md §2-4.
"""
from datetime import datetime, timezone

import httpx

from app.backfill import make_event, paged, parse_iso
from app.webhooks.normalizer import sanitize

_SOURCE = "gitlab"
_API = "https://gitlab.com/api/v4"


def commit_to_event(item: dict, profile_id: str, namespace: str) -> dict:
    """/projects/{id}/repository/commits item. The push webhook injects _commit
    and keys on _commit.id (the sha); the REST commit id is that same sha. Title
    is the first message line, mirroring normalizer._extract_title for gitlab."""
    msg = item.get("message", "") or item.get("title", "")
    return make_event(
        profile_id=profile_id, source=_SOURCE, event_type="commit",
        source_event_id=item.get("id", ""),
        title=sanitize(msg.split("\n")[0]),
        occurred_at=parse_iso(item.get("created_at")),
        workspace=namespace, raw=item,
    )


def mr_to_event(item: dict, profile_id: str, namespace: str) -> dict:
    """/projects/{id}/merge_requests item. The webhook keys on
    object_attributes.id (the global MR id); REST "id" is that same global id —
    NOT "iid", which is per-project and would break dedup."""
    return make_event(
        profile_id=profile_id, source=_SOURCE, event_type="merge_request",
        source_event_id=str(item.get("id", "")),
        title=sanitize(item.get("title", "")),
        occurred_at=parse_iso(item.get("updated_at") or item.get("created_at")),
        workspace=namespace, raw=item,
    )


def issue_to_event(item: dict, profile_id: str, namespace: str) -> dict:
    """/projects/{id}/issues item. Keys on the global "id" (matches
    object_attributes.id), event_type "issue" (matches object_kind)."""
    return make_event(
        profile_id=profile_id, source=_SOURCE, event_type="issue",
        source_event_id=str(item.get("id", "")),
        title=sanitize(item.get("title", "")),
        occurred_at=parse_iso(item.get("updated_at") or item.get("created_at")),
        workspace=namespace, raw=item,
    )


async def fetch_events(token: str, profile_id: str, since: datetime) -> list[dict]:
    """Pull commits, MRs and issues updated since `since` across the user's
    projects and map them to normalized events. Project discovery mirrors
    registration.py. See docs/adr-0003-backfill.md §Phase-1."""
    since_iso = since.astimezone(timezone.utc).isoformat()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    events: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        projects = await paged(client, f"{_API}/projects", headers,
                               {"membership": True, "simple": True})
        for proj in projects:
            pid = proj.get("id")
            ns = proj.get("path_with_namespace", "")
            if pid is None:
                continue
            for c in await paged(client, f"{_API}/projects/{pid}/repository/commits", headers,
                                 {"since": since_iso}):
                events.append(commit_to_event(c, profile_id, ns))
            for mr in await paged(client, f"{_API}/projects/{pid}/merge_requests", headers,
                                  {"updated_after": since_iso}):
                events.append(mr_to_event(mr, profile_id, ns))
            for it in await paged(client, f"{_API}/projects/{pid}/issues", headers,
                                  {"updated_after": since_iso}):
                events.append(issue_to_event(it, profile_id, ns))
    return events
