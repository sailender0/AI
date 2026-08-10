"""Live 'assigned to me' state from Jira.

Extracted from app/routes/stats.py so the AI chat and the proactive agent can
read the board without importing an HTTP route module. Doubles as the Jira
connection health probe — a 401/403 from Atlassian flags the integration.
"""
import httpx

from app.auth.oauth import get_valid_token, mark_integration_error


def issue_row(issue: dict) -> dict:
    """Pure mapper: a /search/jql issue -> one 'assigned to me' panel row."""
    f = issue.get("fields") or {}
    sprints = f.get("customfield_10020") or []
    active = next((s.get("name") for s in sprints
                   if isinstance(s, dict) and s.get("state") == "active"), None)
    last = sprints[-1].get("name") if sprints and isinstance(sprints[-1], dict) else None
    return {
        "key":             issue.get("key", ""),
        "summary":         f.get("summary") or "",
        "status":          (f.get("status") or {}).get("name") or "",
        "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key", ""),
        "priority":        (f.get("priority") or {}).get("name") or "",
        "issue_type":      (f.get("issuetype") or {}).get("name") or "",
        "due_date":        f.get("duedate"),
        "created":         f.get("created"),
        "story_points":    f.get("customfield_10016"),
        "sprint":          active or last,
    }


async def fetch_assigned(profile_id: str) -> dict | None:
    """Live 'assigned to me' state from Jira: {site_url, done_7d, issues}, or
    None when the connection is missing/broken. Shared by the API route and the
    AI chat context. Doubles as the connection health probe: a 401/403 from
    Atlassian flags the integration (amber dot + reconnect banner)."""
    token = await get_valid_token(profile_id, "jira")
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            "https://api.atlassian.com/oauth/token/accessible-resources", headers=headers)
        if res.status_code in (401, 403):
            await mark_integration_error(profile_id, "jira")
        if res.status_code != 200 or not res.json():
            return None
        site = res.json()[0]
        r = await client.get(
            f"https://api.atlassian.com/ex/jira/{site['id']}/rest/api/3/search/jql",
            headers=headers,
            params={
                "jql": "assignee = currentUser() AND statusCategory != Done"
                       " ORDER BY priority DESC, updated DESC",
                "maxResults": 50,
                "fields": "summary,status,priority,issuetype,duedate,created,"
                          "customfield_10016,customfield_10020",
            },
        )
        if r.status_code in (401, 403):
            await mark_integration_error(profile_id, "jira")
        if r.status_code != 200:
            return None

        done = await client.post(
            f"https://api.atlassian.com/ex/jira/{site['id']}/rest/api/3/search/approximate-count",
            headers=headers,
            json={"jql": "assignee = currentUser() AND statusCategory = Done AND resolved >= -7d"},
        )
        done_7d = done.json().get("count") if done.status_code == 200 else None

    return {
        "site_url": site.get("url", ""),
        "done_7d": done_7d,
        "issues": [issue_row(it) for it in r.json().get("issues", [])],
    }
