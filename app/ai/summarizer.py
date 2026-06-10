"""
Scheduled AI summarization — daily (5 PM) and weekly (Friday 5 PM).
Events are fetched from MongoDB, truncated if over budget, sent to Azure OpenAI.
"""
import logging
from datetime import datetime, timedelta, timezone

from openai import AsyncAzureOpenAI
from sqlalchemy import select

from app.config import settings
from app.storage.models import Integration, Profile, Summary
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal

logger = logging.getLogger(__name__)

MAX_EVENTS = 200

_PRIORITY = {
    "pr_merged": 0,
    "commit": 1,
    "ticket_updated": 2,
    "message_sent": 3,
    "meeting": 4,
}


def _openai_client() -> AsyncAzureOpenAI:
    return AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_key=settings.AZURE_OPENAI_KEY,
        api_version="2024-08-01-preview",
    )


def _period_bounds(tz_name: str, period_type: str) -> tuple[datetime, datetime]:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name or "UTC")
    now = datetime.now(tz)
    if period_type == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    else:  # weekly
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _truncate(events: list[dict]) -> list[dict]:
    events.sort(key=lambda e: (_PRIORITY.get(e.get("event_type", ""), 99), -(e["occurred_at"].timestamp() if isinstance(e.get("occurred_at"), datetime) else 0)))
    return events[:MAX_EVENTS]


def _format_events(events: list[dict]) -> str:
    lines = []
    for e in events:
        ts = e.get("occurred_at", "")
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        workspace = e.get("workspace") or ""
        repo_tag = f" [repo:{workspace}]" if workspace else ""
        lines.append(f"[{ts}] [{e.get('source','')}] [{e.get('event_type','')}]{repo_tag} {e.get('title','')}")
    return "\n".join(lines)


def _build_prompt(period_type: str, events: list[dict], caveat: str) -> str:
    return (
        f"You are a personal work assistant summarising a developer's activity.\n"
        f"Write a concise {period_type} summary under 200 words.\n"
        f"Group by workspace if multiple workspaces exist.\n"
        f"Highlight completed work, blockers, and upcoming deadlines.\n"
        f"Do not invent anything not present in the data.\n"
        f"{caveat}\n\n"
        f"ACTIVITY DATA START\n"
        f"{_format_events(events)}\n"
        f"ACTIVITY DATA END\n\n"
        f"Plain text only. No markdown."
    )


async def _get_failed_sources(profile_id: str) -> list[str]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Integration).where(
                    Integration.profile_id == profile_id,
                    Integration.sync_status == "error",
                )
            )
        ).scalars().all()
    return [r.source for r in rows]


async def run_summary_job(period_type: str):
    """Entry point called by APScheduler."""
    async with AsyncSessionLocal() as db:
        profiles = (await db.execute(select(Profile))).scalars().all()

    for profile in profiles:
        profile_id = str(profile.id)
        try:
            await _summarise_profile(profile, profile_id, period_type)
        except Exception as exc:
            logger.error("Summary job failed for %s: %s", profile_id, exc)


async def _summarise_profile(profile, profile_id: str, period_type: str):
    period_start, period_end = _period_bounds(profile.timezone, period_type)

    cursor = activity_events().find(
        {
            "profile_id": profile_id,
            "occurred_at": {"$gte": period_start, "$lte": period_end},
        }
    )
    events = await cursor.to_list(length=MAX_EVENTS * 2)
    if not events:
        return

    if len(events) > MAX_EVENTS:
        events = _truncate(events)

    failed = await _get_failed_sources(profile_id)
    caveat = f"Note: data from {', '.join(failed)} was unavailable." if failed else ""

    prompt = _build_prompt(period_type, events, caveat)

    client = _openai_client()
    response = await client.chat.completions.create(
        model=settings.AZURE_OPENAI_DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0.3,
    )
    summary_text = response.choices[0].message.content.strip()

    async with AsyncSessionLocal() as db:
        summary = Summary(
            profile_id=profile_id,
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
            content=summary_text,
        )
        db.add(summary)
        await db.commit()
        await db.refresh(summary)

    from app.delivery.teams_delivery import deliver_to_teams
    await deliver_to_teams(profile, summary_text)
