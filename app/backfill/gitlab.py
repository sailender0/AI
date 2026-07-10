"""GitLab REST item -> normalized event.

Each mapper reproduces the (source_event_id, event_type, title) the live webhook
receiver produces for the same object, so backfilled rows dedup against webhook
rows. See docs/adr-0003-backfill.md §2-4.
"""
from app.backfill import make_event, parse_iso
from app.webhooks.normalizer import sanitize

_SOURCE = "gitlab"


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
