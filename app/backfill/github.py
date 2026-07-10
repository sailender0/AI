"""GitHub REST item -> normalized event.

Each mapper reproduces the (source_event_id, event_type, title) the live webhook
receiver produces for the same object, so backfilled rows dedup against webhook
rows. See docs/adr-0003-backfill.md §2-4.
"""
from app.backfill import make_event, parse_iso
from app.webhooks.normalizer import sanitize

_SOURCE = "github"


def commit_to_event(item: dict, profile_id: str, repo: str) -> dict:
    """/repos/{repo}/commits item. The push webhook keys the event on the head
    commit sha (raw["after"]); each REST commit keys on its own sha, so the head
    commit dedups and the rest are net-new (ADR-0003 §3, per-commit)."""
    commit = item.get("commit") or {}
    return make_event(
        profile_id=profile_id, source=_SOURCE, event_type="commit",
        source_event_id=item.get("sha", ""),
        title=sanitize(commit.get("message", "")),
        occurred_at=parse_iso((commit.get("author") or {}).get("date")),
        workspace=repo, raw=item,
    )


def pull_to_event(item: dict, profile_id: str, repo: str) -> dict:
    """/repos/{repo}/pulls item. Snapshot -> transition type: merged => pr_merged,
    open => pr_opened, closed-not-merged => pr_closed — matching
    normalizer._map_event_type on the equivalent pull_request webhook action.
    id is the PR's global id (NOT the issue id from the /issues list)."""
    if item.get("merged_at"):
        et = "pr_merged"
    elif item.get("state") == "open":
        et = "pr_opened"
    else:
        et = "pr_closed"
    return make_event(
        profile_id=profile_id, source=_SOURCE, event_type=et,
        source_event_id=str(item.get("id", "")),
        title=sanitize(item.get("title", "")),
        occurred_at=parse_iso(item.get("updated_at") or item.get("created_at")),
        workspace=repo, raw=item,
    )


def issue_to_event(item: dict, profile_id: str, repo: str) -> dict:
    """/repos/{repo}/issues item. That list also returns PRs; callers must route
    is_pull_request() items to pull_to_event (which uses the PR's own id)."""
    return make_event(
        profile_id=profile_id, source=_SOURCE, event_type="issue_updated",
        source_event_id=str(item.get("id", "")),
        title=sanitize(item.get("title", "")),
        occurred_at=parse_iso(item.get("updated_at") or item.get("created_at")),
        workspace=repo, raw=item,
    )


def is_pull_request(issue_item: dict) -> bool:
    """GitHub's /issues list includes PRs, tagged with a "pull_request" key."""
    return "pull_request" in issue_item
