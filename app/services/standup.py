"""Standup generation — builds a standup from real activity data for any date.

Extracted from app/routes/standup.py so the email report can generate a standup
without importing an HTTP route module. Owns the cache read/write in `standups`.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import llm
from app.services.activity_query import compute_focus_blocks, get_profile_tz
from app.services.timezone import day_bounds, now_local, resolve
from app.storage.mongodb import (
    activity_events, ai_tool_events, claude_usage,
    device_heartbeats, local_commits, standups,
)

log = logging.getLogger(__name__)


def _load_instructions() -> str:
    return llm.load_prompt(
        "standup_instructions.txt",
        "Generate a brief engineering standup with bullets: Yesterday / Today / Blockers.",
    )


def yesterday_date(tz_name: str) -> str:
    """Return the local date string for yesterday (Friday if today is Monday)."""
    today = now_local(resolve(tz_name))
    days_back = 3 if today.weekday() == 0 else 1
    return (today - timedelta(days=days_back)).strftime("%Y-%m-%d")


def _date_bounds(date_str: str, tz_name: str) -> tuple[datetime, datetime, str]:
    """Return (utc_start, utc_end, day_label) for a YYYY-MM-DD local date."""
    tzinfo     = resolve(tz_name)
    start, end = day_bounds(date_str, tzinfo)
    local_date = datetime.strptime(date_str, "%Y-%m-%d")
    today_wd   = datetime.now(tzinfo).weekday()
    label      = "Friday" if (today_wd == 0 and date_str == yesterday_date(tz_name)) \
                 else local_date.strftime("%A, %b %-d")
    return start, end, label


async def _build_context(profile_id: str, start: datetime, end: datetime, yest_date: str) -> str:
    ts_filter = {"$gte": start, "$lt": end}
    lines: list[str] = []

    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": ts_filter, "idle": False},
        projection={"git_repo": 1, "timestamp": 1, "_id": 0},
    ).sort("timestamp", 1).to_list(5_000)
    if hbs:
        focus_min = sum(b["duration_min"] for b in compute_focus_blocks(hbs))
        h, m      = divmod(focus_min, 60)
        repos     = sorted({hb["git_repo"] for hb in hbs if hb.get("git_repo")})
        lines.append(f"Focus time: {h}h {m}m")
        if repos:
            lines.append(f"Repos with active coding: {', '.join(repos)}")

    commits = await local_commits().find(
        {"profile_id": profile_id, "timestamp": ts_filter},
        projection={"repo": 1, "branch": 1, "message": 1, "files_changed": 1, "_id": 0},
    ).sort("timestamp", -1).to_list(20)
    if commits:
        lines.append(f"\nLocal commits ({len(commits)}):")
        for c in commits:
            lines.append(
                f"  [{c.get('repo','?')}/{c.get('branch','?')}] "
                f"{c.get('message','')[:120]} ({c.get('files_changed',0)} files)"
            )

    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": ts_filter},
        projection={"source": 1, "event_type": 1, "title": 1, "_id": 0},
    ).sort("occurred_at", -1).to_list(30)
    if events:
        lines.append(f"\nActivity events ({len(events)}):")
        for e in events:
            lines.append(f"  [{e.get('source')}] {e.get('event_type','')}: {(e.get('title') or '')[:100]}")

    claude_docs = await claude_usage().find(
        {"profile_id": profile_id, "date": yest_date}
    ).to_list(50)
    if claude_docs:
        repos = sorted({d.get("repo","") for d in claude_docs if d.get("repo")})
        lines.append(f"\nClaude Code used on: {', '.join(repos) or 'unknown repo'}")

    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": ts_filter},
        projection={"tools": 1, "_id": 0},
    ).to_list(500)
    if ai_docs:
        all_tools: set[str] = set()
        for doc in ai_docs:
            all_tools.update(doc.get("tools", []))
        if all_tools:
            lines.append(f"\nAI tools detected: {', '.join(sorted(all_tools))}")

    return "\n".join(lines) if lines else "No activity data found for this date."


async def generate(profile_id: str, db: AsyncSession, target_date: str | None = None) -> dict:
    """The standup for `target_date` (default: yesterday), served from the
    `standups` cache when present and generated via the LLM otherwise."""
    tz_name    = await get_profile_tz(profile_id, db)
    date_str   = target_date or yesterday_date(tz_name)
    start, end, day_label = _date_bounds(date_str, tz_name)

    cached = await standups().find_one({"profile_id": profile_id, "date": date_str})
    if cached:
        gen_at = cached.get("generated_at")
        return {
            "standup": cached["text"],
            "cached": True,
            "date": date_str,
            "period": day_label,
            "generated_at": gen_at.isoformat() if gen_at else None,
        }

    context = await _build_context(profile_id, start, end, date_str)
    log.info("Standup context for %s (%s): %s…", profile_id, date_str, context[:300])

    try:
        text = await llm.answer(
            _load_instructions(),
            f"Here is my activity data for {day_label}:\n\n{context}",
            max_tokens=220,
            temperature=0.3,
        )
    except Exception as exc:
        log.error("Standup AI call failed: %s", exc)
        raise

    gen_at = datetime.now(timezone.utc)
    await standups().update_one(
        {"profile_id": profile_id, "date": date_str},
        {"$set": {"text": text, "generated_at": gen_at}},
        upsert=True,
    )
    return {
        "standup": text,
        "cached": False,
        "date": date_str,
        "period": day_label,
        "generated_at": gen_at.isoformat(),
    }
