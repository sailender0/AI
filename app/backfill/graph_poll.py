"""The hourly sweep that runs every Graph connector.

Lives outside the individual connectors on purpose: a job that drives three of
them shouldn't sit inside one, and importing sideways between connectors would
put a cycle in the routes -> services -> storage layering the arch test guards.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.backfill.outlook_calendar import backfill_calendar_day
from app.backfill.outlook_mail import backfill_mail_day
from app.backfill.teams_calls import run_call_poll
from app.backfill.teams_chat import backfill_chat_day
from app.storage.models import Profile
from app.storage.postgres import AsyncSessionLocal

logger = logging.getLogger(__name__)


def poll_days(now_local: datetime) -> list[str]:
    """Which local days to re-poll on each run.

    Today, plus yesterday for the first few hours after midnight — a message sent
    at 23:58 can easily land after the last run of the previous day, and
    re-polling is free because ingest() dedups on the Graph id.
    """
    days = [now_local.strftime("%Y-%m-%d")]
    if now_local.hour < 3:
        days.append((now_local - timedelta(days=1)).strftime("%Y-%m-%d"))
    return days


async def run_graph_poll_job():
    """APScheduler entry (hourly). Pulls chat, mail and meetings for every
    profile's own local today.

    Each connector no-ops on a missing token or an unconsented scope, and each
    call is wrapped so one profile's bad token, throttling or outage never stops
    the sweep — a 429 on mail must not cost everyone their chat rows.
    """
    now_utc = datetime.now(timezone.utc)
    for day in poll_days(now_utc):
        try:
            logger.info("graph poll: calls %s -> %d new", day, await run_call_poll(day))
        except Exception:
            logger.exception("call poll failed on %s", day)

    async with AsyncSessionLocal() as db:
        profiles = (await db.execute(select(Profile))).scalars().all()

    for profile in profiles:
        pid = str(profile.id)
        now_local = datetime.now(ZoneInfo(profile.timezone or "UTC"))
        for day in poll_days(now_local):
            counts = {}
            for name, run in (("chat", backfill_chat_day),
                              ("mail", backfill_mail_day),
                              ("calendar", backfill_calendar_day)):
                if name == "chat" and not profile.teams_user_id:
                    continue
                try:
                    counts[name] = await run(pid, day)
                except Exception:
                    logger.exception("%s poll failed for %s on %s", name, pid, day)
            logger.info("graph poll: %s %s -> %s", profile.email or pid, day, counts)
