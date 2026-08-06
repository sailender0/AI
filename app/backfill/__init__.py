"""Event backfill — reconstruct activity from connector REST APIs.

The mappers (github.py, gitlab.py, jira.py and the Graph connectors) produce the
SAME normalized shape and (source_event_id, event_type) as the live webhook path,
so app.webhooks.normalizer.ingest() deduplicates a backfilled row against its
webhook twin via the unique index. See docs/adr-0003-backfill.md.

runner.py drives token -> fetch -> ingest and is reachable from
POST /api/backfill/{source}; the per-source fetch is injectable so the
orchestration is testable without a live API. This module holds only what the
connectors share: pagination, Graph helpers, and the event constructor.
"""
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def graph_ts(ts: datetime) -> str:
    """A UTC instant as the literal Graph's $filter expects.

    Always feed this the output of services.timezone.day_bounds, never a bare
    date. Graph filters are UTC but the poll asks for a day in the profile's own
    zone; pasting that date straight into a `...Z` literal queried the wrong
    window — behind UTC (America/Los_Angeles) everything after ~17:00 local
    carries tomorrow's UTC date and went unseen until after local midnight, and
    ahead of UTC (Asia/Kolkata) the early-morning hours carry yesterday's date,
    which nothing re-polls after 03:00, so those were lost outright.
    """
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(raw_ts) -> datetime:
    """The same lenient ISO parse the normalizer uses; falls back to now()."""
    if raw_ts:
        try:
            return datetime.fromisoformat(str(raw_ts))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def paged(client, url: str, headers: dict, params: dict, *, cap: int = 10) -> list:
    """GET `url` paging via ?per_page=100&page=N until a short/empty page or cap.
    Works for GitHub and GitLab (both accept page/per_page). `client` is any
    object with an async .get(url, headers, params) returning .status_code/.json()."""
    page, out = 1, []
    while page <= cap:
        r = await client.get(url, headers=headers, params={**params, "per_page": 100, "page": page})
        if getattr(r, "status_code", None) != 200:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


GRAPH = "https://graph.microsoft.com/v1.0"


def person_key(email: str | None, fallback: str) -> str:
    """The id a person is filed under, shared by every Graph connector.

    Lowercased email address wherever we have one, because that is the ONLY
    identifier mail and calendar ever expose — keying chat on the Entra oid would
    file the same colleague under two ids and split their history in the person
    search. Falls back to the oid when no address is available (a chat member
    beyond the 25 the members expansion returns, or a non-Entra participant).
    """
    return email.strip().lower() if email and email.strip() else fallback


def addr_of(entry: dict) -> tuple[str, str]:
    """(address, display name) out of a Graph emailAddress wrapper.

    Mail, calendar and every other Graph payload nest a person the same way, so
    the unwrap lives here rather than once per connector.
    """
    e = (entry or {}).get("emailAddress") or {}
    return (e.get("address") or "").strip(), (e.get("name") or "").strip()


async def walk(client, url: str, headers: dict, params: dict | None, *, cap: int = 20) -> list:
    """Follow @odata.nextLink through a Graph collection. Graph bakes the query
    into nextLink, so params go on the first request only.
    """
    out = []
    for _ in range(cap):
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            try:
                err = (resp.json() or {}).get("error") or {}
                detail = f'{err.get("code", "?")}: {str(err.get("message", ""))[:200]}'
            except Exception:
                detail = "<unparseable error body>"
            logger.warning("Graph %s on %s — %s", resp.status_code,
                           str(url).split("?")[0], detail)
            break
        data = resp.json()
        out.extend(data.get("value", []))
        url, params = data.get("@odata.nextLink"), None
        if not url:
            break
    return out


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
