"""
TOOL-CALLING PROTOTYPE (isolated) — POST /api/chat/ask/tools
The streaming chat pre-fetches a fixed context + keyword gates. This path instead
hands the model a few parameterized tools and lets it choose and COMPOSE them (call
the same tool twice with different periods to compare). Non-streaming, no
conversation persistence — a sandbox to A/B against the pipeline. The per-tool
period helpers are also reused by insights.py.
"""
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.ai import llm
from app.ai.context import _load_instructions, _sanitize_question
from app.ai.summarizer import _format_events
from app.auth.sso import require_profile
from app.services.activity_query import compute_focus_blocks
from app.services.device_analytics import period_ranges, tool_active_minutes
from app.services.timezone import day_bounds, now_local, resolve
from app.storage.models import Profile
from app.storage.mongodb import (
    activity_events, device_heartbeats, claude_usage, ai_tool_events, local_commits,
)
from app.storage.postgres import AsyncSessionLocal

router = APIRouter()
logger = logging.getLogger(__name__)

_PERIOD_ENUM = ["today", "this_week", "last_week", "this_month", "last_month", "last_7_days"]


def _resolve_period(period: str, tz_name: str, today: date | None = None) -> tuple[str, str]:
    """Period token → (from, to) local date strings. Pure when `today` is supplied
    (that path is what tests/test_ai_tools_period.py exercises)."""
    if today is None:
        today = now_local(resolve(tz_name)).date()
    if period == "today":
        return (today.isoformat(), today.isoformat())
    if period == "last_7_days":
        return ((today - timedelta(days=6)).isoformat(), today.isoformat())
    for gran in ("week", "month"):
        r = period_ranges(gran, today)
        if period == f"this_{gran}":
            return r["this"]
        if period == f"last_{gran}":
            return r["last"]
    return period_ranges("week", today)["this"]        # unknown → this week


def _period_ts_filter(frm: str, to: str, tz_name: str) -> dict:
    """(from, to) local date strings → a Mongo timestamp range on local-midnight bounds."""
    tz = resolve(tz_name)
    start, _ = day_bounds(frm, tz)
    _, end   = day_bounds(to, tz)
    return {"$gte": start, "$lte": end}


# ── Tool bodies — each a thin wrapper over a fetch we already have ──────────────

async def _tool_get_activity(profile_id, tz_name, args) -> dict:
    frm, to = _resolve_period(args.get("period", "this_week"), tz_name)
    flt = {"profile_id": profile_id, "occurred_at": _period_ts_filter(frm, to, tz_name)}
    source = args.get("source")
    if source and source != "null":
        flt["source"] = source
    events = await activity_events().find(flt).to_list(100)
    by_source: dict[str, int] = {}
    for e in events:
        s = e.get("source", "?")
        by_source[s] = by_source.get(s, 0) + 1
    return {"period": f"{frm} to {to}", "total_events": len(events),
            "by_source": by_source, "sample": _format_events(events[:40])}


async def _tool_get_token_usage(profile_id, tz_name, args) -> dict:
    frm, to = _resolve_period(args.get("period", "this_week"), tz_name)
    docs = await claude_usage().find(
        {"profile_id": profile_id, "date": {"$gte": frm, "$lte": to}}
    ).to_list(500)
    tin  = sum(d.get("input_tokens", 0) for d in docs)
    tout = sum(d.get("output_tokens", 0) for d in docs)
    out = {"period": f"{frm} to {to}", "total_tokens": tin + tout,
           "input_tokens": tin, "output_tokens": tout}
    group_by = args.get("group_by")
    if group_by == "repo":
        repos: dict[str, int] = {}
        for d in docs:
            r = d.get("repo") or "unknown"
            repos[r] = repos.get(r, 0) + d.get("input_tokens", 0) + d.get("output_tokens", 0)
        out["by_repo"] = repos
    elif group_by == "day":
        days: dict[str, int] = {}
        for d in docs:
            dk = d.get("date", "")
            days[dk] = days.get(dk, 0) + d.get("input_tokens", 0) + d.get("output_tokens", 0)
        out["by_day"] = days
    return out


async def _tool_get_ai_tools(profile_id, tz_name, args) -> dict:
    frm, to = _resolve_period(args.get("period", "this_week"), tz_name)
    ts = _period_ts_filter(frm, to, tz_name)
    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": ts, "idle": False},
        projection={"timestamp": 1, "_id": 0},
    ).sort("timestamp", 1).to_list(35_000)
    focus_blocks = compute_focus_blocks(hbs)
    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": ts},
        projection={"tools": 1, "timestamp": 1, "_id": 0},
    ).to_list(2000)
    active_min = tool_active_minutes(ai_docs, focus_blocks)
    all_tools: set[str] = set()
    for d in ai_docs:
        all_tools.update(d.get("tools", []))
    return {"period": f"{frm} to {to}",
            "active_minutes_by_tool": {t: active_min.get(t, 0) for t in sorted(all_tools)},
            "note": "minutes = active (non-idle) time; token counts exist only for claude-code"}


async def _tool_get_focus_time(profile_id, tz_name, args) -> dict:
    frm, to = _resolve_period(args.get("period", "this_week"), tz_name)
    ts = _period_ts_filter(frm, to, tz_name)
    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": ts, "idle": False},
        projection={"timestamp": 1, "_id": 0},
    ).sort("timestamp", 1).to_list(35_000)
    focus_min = sum(b["duration_min"] for b in compute_focus_blocks(hbs))
    commits = await local_commits().count_documents({"profile_id": profile_id, "timestamp": ts})
    return {"period": f"{frm} to {to}", "focus_minutes": focus_min, "local_commits": commits}


_TOOL_FNS = {
    "get_activity":    _tool_get_activity,
    "get_token_usage": _tool_get_token_usage,
    "get_ai_tools":    _tool_get_ai_tools,
    "get_focus_time":  _tool_get_focus_time,
}

_period_param = {"type": "string", "enum": _PERIOD_ENUM}

_TOOLS = [
    {"type": "function", "function": {
        "name": "get_activity",
        "description": "GitHub/GitLab/Jira/Teams activity events for a period. "
                       "Call twice with different periods to compare.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"period": _period_param,
                           "source": {"type": ["string", "null"],
                                      "enum": ["github", "gitlab", "jira", "teams", None]}},
            "required": ["period", "source"]}}},
    {"type": "function", "function": {
        "name": "get_token_usage",
        "description": "Claude Code token totals for a period (claude-code only). Call twice "
                       "to compare periods; group_by='repo' or 'day' for a breakdown.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"period": _period_param,
                           "group_by": {"type": ["string", "null"], "enum": ["repo", "day", None]}},
            "required": ["period", "group_by"]}}},
    {"type": "function", "function": {
        "name": "get_ai_tools",
        "description": "Which AI coding apps ran and their active minutes for a period "
                       "(claude-code, github-copilot, cursor-ai, ...). Use to compare apps by usage time.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"period": _period_param}, "required": ["period"]}}},
    {"type": "function", "function": {
        "name": "get_focus_time",
        "description": "Focus/coding minutes and local commit count for a period.",
        "parameters": {"type": "object", "additionalProperties": False,
            "properties": {"period": _period_param}, "required": ["period"]}}},
]

_TOOLS_PREAMBLE = (
    "You answer questions about the user's developer activity by calling tools. "
    "To compare two periods, call the SAME tool twice with different `period` values and "
    "compute the difference yourself. Token counts exist only for claude-code; other apps "
    "have active-time only, so never report token counts for them.\n\n"
)


async def _run_tool(name: str, args: dict, profile_id: str, tz_name: str) -> dict:
    fn = _TOOL_FNS.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    try:
        return await fn(profile_id, tz_name, args)
    except Exception as exc:                            # a tool failure shouldn't kill the turn
        logger.warning("tool %s failed: %s", name, exc)
        return {"error": str(exc)}


class AskToolsRequest(BaseModel):
    question: str
    tz: str | None = None


@router.post("/api/chat/ask/tools")
async def ask_with_tools(body: AskToolsRequest, profile_id: str = Depends(require_profile)):
    """Isolated tool-calling prototype — the model picks and combines tools instead of
    receiving a pre-fetched context. Non-streaming; no conversation persistence."""
    question = _sanitize_question((body.question or "").strip())
    if not question:
        return JSONResponse({"error": "question_required"}, status_code=400)

    async with AsyncSessionLocal() as db:
        profile = await db.get(Profile, profile_id)
        profile_tz = (profile.timezone or "UTC") if profile else "UTC"
    tz_name = resolve(profile_tz, body.tz).key

    async def dispatch(name, args):
        return await _run_tool(name, args, profile_id, tz_name)

    try:
        answer = await llm.answer_with_tools(
            _TOOLS_PREAMBLE + _load_instructions(), question, _TOOLS, dispatch,
            max_tokens=500, temperature=0.3,
        )
    except Exception as exc:
        logger.error("ask_with_tools failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"answer": answer})
