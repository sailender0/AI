"""Event backfill — reconstruct activity from connector REST APIs.

Only the pure REST->event mappers live here for now (github.py, gitlab.py).
They produce the SAME normalized shape and (source_event_id, event_type) as the
live webhook path, so app.webhooks.normalizer.ingest() deduplicates a backfilled
row against its webhook twin via the unique index. See docs/adr-0003-backfill.md.

The fetch/runner/route layer talks to live GitHub/GitLab APIs and needs real
tokens to verify, so it is deferred until creds are available (ADR Phase-1
remainder). These mappers are the correctness-critical, offline-testable core.
"""
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def parse_iso(raw_ts) -> datetime:
    """The same lenient ISO parse the normalizer uses; falls back to now()."""
    if raw_ts:
        try:
            return datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def paged(client, url: str, headers: dict, params: dict, *, cap: int = 10) -> list:
    """GET `url` paging via ?per_page=100&page=N until a short/empty page or cap.
    Works for GitHub and GitLab (both accept page/per_page). `client` is any
    object with an async .get(url, headers, params) returning .status_code/.json().

    ponytail: cap bounds a runaway backfill at cap*100 items/endpoint; raise cap
    if a real backfill needs deeper history than ~1000 items per repo/endpoint."""
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


async def walk(client, url: str, headers: dict, params: dict | None, *, cap: int = 20) -> list:
    """Follow @odata.nextLink through a Graph collection. Graph bakes the query
    into nextLink, so params go on the first request only.

    ponytail: cap bounds a runaway walk at cap*50 items; raise it if a real day
    legitimately carries more than ~1000 messages or events.
    """
    out = []
    for _ in range(cap):
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            # Log the API error only — code and message, never resp.text, which
            # on a 200 would carry message bodies. Without this a bad $filter or
            # a missing scope fails completely silently.
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
