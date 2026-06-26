"""
Scheduled AI summarization — daily (11:59 PM local time) and weekly (Friday 11:59 PM).
Events are fetched from MongoDB, truncated if over budget, sent to Azure OpenAI.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


_client: AsyncAzureOpenAI | None = None


def _openai_client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
        _client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_KEY,
            api_version="2024-08-01-preview",
            max_retries=3,
        )
    return _client


def _period_bounds(tz_name: str, period_type: str, full_day: bool = False) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name or "UTC")
    now = datetime.now(tz)
    if period_type == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Scheduled job captures the full day (00:00–23:59:59); manual captures up to now
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999) if full_day else now
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
        f"Structure the output as follows:\n"
        f"  - Group activities by integration (GitHub, GitLab, Jira, Teams).\n"
        f"  - For each integration, write the integration name on its own line (e.g. 'GitHub').\n"
        f"  - Under each integration, group by workspace on its own line (e.g. 'Workspace: owner/repo').\n"
        f"  - List the activity items as bullet points under each workspace.\n"
        f"  - Skip any integration that has no activity.\n"
        f"Highlight completed work, blockers, and upcoming deadlines.\n"
        f"End with a single line about blockers/deadlines (or state there are none).\n"
        f"Do not invent anything not present in the data.\n"
        f"{caveat}\n\n"
        f"ACTIVITY DATA START\n"
        f"{_format_events(events)}\n"
        f"ACTIVITY DATA END\n\n"
        f"Plain text only. No markdown symbols like **, ##, or *."
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
    """Entry point called by APScheduler (fires every hour at :59)."""
    async with AsyncSessionLocal() as db:
        profiles = (await db.execute(select(Profile))).scalars().all()

    for profile in profiles:
        profile_id = str(profile.id)
        try:
            if period_type == "daily":
                # Only generate when it is 23:xx in the user's local timezone
                local_now = datetime.now(ZoneInfo(profile.timezone or "UTC"))
                if local_now.hour != 23:
                    continue
            await _summarise_profile(profile, profile_id, period_type, full_day=(period_type == "daily"))
        except Exception as exc:
            logger.error("Summary job failed for %s: %s", profile_id, exc)


async def _summarise_profile(
    profile,
    profile_id: str,
    period_type: str,
    full_day: bool = False,
    specific_date: str = None,
    period_start_utc: datetime | None = None,
    period_end_utc: datetime | None = None,
):
    if period_start_utc and period_end_utc:
        period_start, period_end = period_start_utc, period_end_utc
    elif specific_date and period_type == "daily":
        tz = ZoneInfo(profile.timezone or "UTC")
        sd = datetime.strptime(specific_date, "%Y-%m-%d").replace(tzinfo=tz)
        period_start = sd.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        period_end   = (sd + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    else:
        period_start, period_end = _period_bounds(profile.timezone, period_type, full_day)

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
    logger.info("Summary generated — profile=%s period=%s", profile_id, period_type)

    async with AsyncSessionLocal() as db:
        # Anchor window to UTC day boundary so entries stored before/after timezone fix
        # (e.g. Jun 15 00:00 UTC vs Jun 15 07:00 UTC) are treated as the same period
        if period_type == "daily":
            window_start = period_start.replace(hour=0, minute=0, second=0, microsecond=0)
            window_end   = window_start + timedelta(days=1)
        else:
            window_start = period_start.replace(hour=0, minute=0, second=0, microsecond=0)
            window_end   = window_start + timedelta(weeks=1)

        existing_rows = (await db.execute(
            select(Summary).where(
                Summary.profile_id == profile_id,
                Summary.period_type == period_type,
                Summary.period_start >= window_start,
                Summary.period_start <  window_end,
            ).order_by(Summary.period_end.desc())
        )).scalars().all()

        if existing_rows:
            # Keep the newest, delete any duplicates created by earlier timezone mismatches
            summary = existing_rows[0]
            for dupe in existing_rows[1:]:
                await db.delete(dupe)
            summary.content      = summary_text
            summary.period_start = period_start
            summary.period_end   = period_end
        else:
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


async def run_startup_catchup():
    """
    Called once on startup. For each profile, generates any daily or weekly
    summaries that were missed while the app was offline.

    Daily:  generates yesterday's summary if it doesn't exist yet.
    Weekly: generates last week's summary if the Friday 17:00 trigger window
            has already passed but no summary was saved for that week.
    """
    async with AsyncSessionLocal() as db:
        profiles = (await db.execute(select(Profile))).scalars().all()

    for profile in profiles:
        profile_id = str(profile.id)
        tz         = ZoneInfo(profile.timezone or "UTC")
        now        = datetime.now(tz)

        # ── Missed daily: yesterday ───────────────────────────────────────
        try:
            ydate  = (now - timedelta(days=1)).date()
            ystart = datetime(ydate.year, ydate.month, ydate.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
            yend   = ystart + timedelta(days=1)

            async with AsyncSessionLocal() as db:
                has_daily = (await db.execute(
                    select(Summary.id).where(
                        Summary.profile_id == profile_id,
                        Summary.period_type == "daily",
                        Summary.period_start >= ystart,
                        Summary.period_start <  yend,
                    )
                )).scalar_one_or_none()

            if not has_daily:
                await _summarise_profile(
                    profile, profile_id, "daily",
                    full_day=True,
                    specific_date=ydate.isoformat(),
                )
                logger.info("Startup catch-up: daily summary generated for %s (%s)", profile_id, ydate)
        except Exception as exc:
            logger.error("Startup catch-up daily failed for %s: %s", profile_id, exc)

        # ── Missed weekly: last Friday's window ───────────────────────────
        try:
            dow       = now.weekday()            # 0=Mon … 4=Fri … 6=Sun
            days_back = (dow - 4) % 7            # days since last Friday
            if days_back == 0 and now.hour < 17:
                days_back = 7                    # It's Friday but before the 17:00 trigger

            if days_back == 0 and now.hour >= 17:
                # It IS Friday at or past trigger time — current week is the target
                mon = now.date() - timedelta(days=dow)
            elif days_back > 0:
                last_fri = now.date() - timedelta(days=days_back)
                mon      = last_fri - timedelta(days=last_fri.weekday())
            else:
                continue  # Friday before 17:00 — no missed window yet

            wstart = datetime(mon.year, mon.month, mon.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
            wend   = wstart + timedelta(days=7)

            async with AsyncSessionLocal() as db:
                has_weekly = (await db.execute(
                    select(Summary.id).where(
                        Summary.profile_id == profile_id,
                        Summary.period_type == "weekly",
                        Summary.period_start >= wstart,
                        Summary.period_start <  wend,
                    )
                )).scalar_one_or_none()

            if not has_weekly:
                await _summarise_profile(
                    profile, profile_id, "weekly",
                    period_start_utc=wstart,
                    period_end_utc=wend,
                )
                logger.info("Startup catch-up: weekly summary generated for %s (week of %s)", profile_id, mon)
        except Exception as exc:
            logger.error("Startup catch-up weekly failed for %s: %s", profile_id, exc)
