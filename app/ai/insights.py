"""
PROACTIVE AGENT — GET /api/agent/insights
The floating bubble (base.html) no longer chats; it shows what the agent thinks
needs attention: overdue Jira, a token spike, an idle-day nudge, and a one-line
LLM digest of the day. Every fetch here is reused from the chat pipeline
(context.py) and the tool prototype (tools.py).
"""
import logging
import time
from datetime import date, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.ai import llm
from app.ai.context import _fetch_my_activity_context
from app.ai.summarizer import _format_events
from app.ai.tools import _tool_get_focus_time
from app.auth.sso import require_profile
from app.services.device_analytics import period_ranges, token_total
from app.services.jira_board import fetch_assigned
from app.services.timezone import day_bounds, now_local, resolve
from app.storage.models import Profile
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal

router = APIRouter()
logger = logging.getLogger(__name__)

_INSIGHTS_TTL = 600
_insights_cache: dict[str, tuple[float, dict]] = {}


def _jira_due_buckets(assigned: dict, today: date) -> tuple[list[str], list[str]]:
    """(overdue_keys, due_within_3_days_keys) from the live assigned snapshot."""
    overdue: list[str] = []
    due_soon: list[str] = []
    for it in assigned.get("issues", []):
        raw = it.get("due_date")
        if not raw:
            continue
        try:
            due = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (due - today).days
        if delta < 0:
            overdue.append(it.get("key", "?"))
        elif delta <= 3:
            due_soon.append(it.get("key", "?"))
    return overdue, due_soon


def _keys_phrase(keys: list[str]) -> str:
    """'AI-1, AI-2, AI-3' — capped at 3 with '+N more' so the card stays one line."""
    shown = ", ".join(keys[:3])
    extra = len(keys) - 3
    return f"{shown} +{extra} more" if extra > 0 else shown


async def _daily_digest(profile_id: str, tz_name: str, today: date) -> str:
    """One friendly sentence about the day so far — the only LLM call on this path."""
    tz = resolve(tz_name)
    time_filter = {"$gte": day_bounds(today.isoformat(), tz)[0]}
    ctx = await _fetch_my_activity_context(profile_id, time_filter, tz_name, "")
    events = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": time_filter}
    ).to_list(100)
    activity_text = _format_events(events) if events else "No integration activity yet today."
    prompt = (
        "Summarise the developer's day so far in ONE short, friendly sentence "
        "(max 25 words). If little has happened, say it's quiet so far — never invent activity.\n\n"
        f"ACTIVITY:\n{activity_text}\n\nDESKTOP/LOCAL:\n{ctx or 'none'}"
    )
    try:
        return (await llm.answer("", prompt, max_tokens=60, temperature=0.4)).strip()
    except Exception as exc:
        logger.warning("daily digest failed: %s", exc)
        return ""


def _shipped_phrase(commits: int, prs: int, issues: int) -> str:
    """'4 commits, 1 PR update' — omits zero counts; '' when nothing shipped."""
    parts = []
    if commits:
        parts.append(f"{commits} commit{'s' if commits != 1 else ''}")
    if prs:
        parts.append(f"{prs} PR update{'s' if prs != 1 else ''}")
    if issues:
        parts.append(f"{issues} issue update{'s' if issues != 1 else ''}")
    return ", ".join(parts)


async def _todays_pr_issue_counts(profile_id: str, tz, today: date) -> tuple[int, int]:
    """(pr_events, issue_events) in the user's activity feed for today. Event-based —
    a PR opened + merged the same day counts twice; fine for a wrap-up nudge."""
    start, end = day_bounds(today.isoformat(), tz)
    docs = await activity_events().find(
        {"profile_id": profile_id, "occurred_at": {"$gte": start, "$lt": end}},
        projection={"event_type": 1, "_id": 0},
    ).to_list(300)
    prs    = sum(1 for d in docs if str(d.get("event_type", "")).startswith("pr_"))
    issues = sum(1 for d in docs if str(d.get("event_type", "")).startswith("issue"))
    return prs, issues


async def _build_insights(profile_id: str, tz_name: str) -> dict:
    """Proactive cards + digest. Each card is {icon, level, href, text}."""
    tz    = resolve(tz_name)
    now   = now_local(tz)
    today = now.date()
    cards: list[dict] = []

    assigned = await fetch_assigned(profile_id)
    if assigned:
        overdue, due_soon = _jira_due_buckets(assigned, today)
        if overdue:
            cards.append({"icon": "🔴", "level": "warn", "href": "/jira",
                          "text": f"Jira overdue ({len(overdue)}): {_keys_phrase(overdue)}"})
        if due_soon:
            cards.append({"icon": "🟠", "level": "info", "href": "/jira",
                          "text": f"Jira due within 3 days ({len(due_soon)}): {_keys_phrase(due_soon)}"})

    rng = period_ranges("week", today)
    (tf, tt), (lf, lt) = rng["this"], rng["last"]
    this_tok = await token_total(profile_id, tf, tt)
    last_tok = await token_total(profile_id, lf, lt)
    if last_tok and this_tok:
        pct = (this_tok - last_tok) / last_tok * 100
        if abs(pct) >= 30:
            cards.append({"icon": "📈" if pct > 0 else "📉", "level": "info",
                          "href": "/my-activity/ai-tools",
                          "text": f"Claude tokens {'up' if pct > 0 else 'down'} {abs(pct):.0f}% vs last week"})

    if now.hour >= 13:
        ft = await _tool_get_focus_time(profile_id, tz_name, {"period": "today"})
        commits   = ft.get("local_commits", 0)
        focus_min = ft.get("focus_minutes", 0)
        prs, issues = await _todays_pr_issue_counts(profile_id, tz, today)

        if not commits and not prs and not issues and focus_min < 30:
            cards.append({"icon": "💤", "level": "info", "href": "/my-activity",
                          "text": "No commits and little focus time logged today"})
        elif now.hour >= 16:
            phrase = _shipped_phrase(commits, prs, issues)
            if phrase:
                cards.append({"icon": "📝", "level": "info", "href": "/",
                              "text": f"Shipped today: {phrase} — generate your standup?"})

    return {"digest": await _daily_digest(profile_id, tz_name, today),
            "cards": cards, "generated_at": now.isoformat()}


@router.get("/api/agent/insights")
async def agent_insights(tz: str | None = None, fresh: bool = False,
                         profile_id: str = Depends(require_profile)):
    """Proactive cards + one-line digest for the floating agent bubble. Cached per
    profile for _INSIGHTS_TTL so the LLM digest stays off the per-page-load path;
    `fresh=1` (the panel's refresh button) bypasses the cache for a live recompute."""
    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
        profile_tz = (profile.timezone or "UTC") if profile else "UTC"
    tz_name = resolve(profile_tz, tz).key

    epoch = time.time()
    if not fresh:
        cached = _insights_cache.get(profile_id)
        if cached and cached[0] > epoch:
            return JSONResponse(cached[1])
    payload = await _build_insights(profile_id, tz_name)
    _insights_cache[profile_id] = (epoch + _INSIGHTS_TTL, payload)
    return JSONResponse(payload)
