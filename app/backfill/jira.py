"""Jira REST item -> normalized event (ADR-0003 Phase 2).

Issues only. The webhook keys a comment_created event on the *issue* id (see
normalizer._extract_native_id), so per-comment backfill can't produce distinct
dedup keys and is low-value; skipped deliberately. Issue snapshots map to
jira:issue_updated — matching the dominant webhook event for the same issue.
"""
from datetime import datetime, timezone

import httpx

from app.backfill import make_event, parse_iso
from app.webhooks.normalizer import sanitize

_SOURCE = "jira"


def issue_to_event(issue: dict, profile_id: str) -> dict:
    """A /search issue. Keys on issue id, event_type jira:issue_updated —
    matching normalizer's output for the equivalent issue webhook."""
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    due = fields.get("duedate")
    return make_event(
        profile_id=profile_id, source=_SOURCE, event_type="jira:issue_updated",
        source_event_id=str(issue.get("id", "")),
        title=sanitize(fields.get("summary", "")),
        occurred_at=parse_iso(fields.get("updated")),
        workspace=project.get("key") or project.get("name"),
        due_date=parse_iso(due) if due else None,
        raw=issue,
    )


# ── Fetch (live API — smoke-test with a real token; contract unverified in CI) ──

async def fetch_events(token: str, profile_id: str, since: datetime) -> list[dict]:
    """Resolve the cloud id, then page issues assigned to the user updated since
    `since`. Jira paginates with startAt/maxResults (not page/per_page), so it
    doesn't use the shared paged() helper. See docs/adr-0003-backfill.md §Phase-2."""
    since_date = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    events: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            "https://api.atlassian.com/oauth/token/accessible-resources", headers=headers)
        if res.status_code != 200 or not res.json():
            return events
        cloud_id = res.json()[0]["id"]
        base = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
        jql = f'assignee = currentUser() AND updated >= "{since_date}" ORDER BY updated DESC'

        start = 0
        while start < 1000:                      # ponytail: cap 1000 issues/backfill
            r = await client.get(
                f"{base}/search", headers=headers,
                params={"jql": jql, "startAt": start, "maxResults": 100,
                        "fields": "summary,project,updated,duedate"})
            if r.status_code != 200:
                break
            data = r.json()
            issues = data.get("issues", [])
            events.extend(issue_to_event(it, profile_id) for it in issues)
            if not issues or start + len(issues) >= data.get("total", 0):
                break
            start += len(issues)
    return events
