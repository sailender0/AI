"""GitHub REST item -> normalized event.

Each mapper reproduces the (source_event_id, event_type, title) the live webhook
receiver produces for the same object, so backfilled rows dedup against webhook
rows. See docs/adr-0003-backfill.md §2-4.
"""
from datetime import datetime, timezone

import httpx

from app.backfill import make_event, paged, parse_iso
from app.webhooks.normalizer import sanitize

_SOURCE = "github"
_API = "https://api.github.com"


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


# ── Fetch (live API — smoke-test with a real token; contract unverified in CI) ──

async def fetch_events(token: str, profile_id: str, since: datetime) -> list[dict]:
    """Pull commits, issues and PRs updated since `since` across the user's repos
    and map them to normalized events. See docs/adr-0003-backfill.md §Phase-1."""
    since_iso = since.astimezone(timezone.utc).isoformat()
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    events: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        repos = await paged(client, f"{_API}/user/repos", headers,
                            {"affiliation": "owner,collaborator,organization_member", "sort": "pushed"})
        for repo in repos:
            full = repo.get("full_name")
            if not full:
                continue
            for c in await paged(client, f"{_API}/repos/{full}/commits", headers, {"since": since_iso}):
                events.append(commit_to_event(c, profile_id, full))
            for it in await paged(client, f"{_API}/repos/{full}/issues", headers,
                                  {"since": since_iso, "state": "all"}):
                if not is_pull_request(it):
                    events.append(issue_to_event(it, profile_id, full))
            # /pulls has no `since`; list is newest-updated first, so stop at the
            # first PR older than the window.
            for pr in await paged(client, f"{_API}/repos/{full}/pulls", headers,
                                  {"state": "all", "sort": "updated", "direction": "desc"}):
                if parse_iso(pr.get("updated_at")) < since:
                    break
                events.append(pull_to_event(pr, profile_id, full))
    return events
