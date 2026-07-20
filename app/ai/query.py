"""
On-demand Q&A endpoint + persistent multi-turn chat conversations.
"""
import logging
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import json as _json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.ai import llm
from app.ai.summarizer import _format_events
from app.auth.sso import require_profile
from app.routes.agent.analytics import (
    _tool_active_minutes, _tool_active_periods, _merge_hourly,
    _period_ranges, _token_total,
)
from app.routes.stats import fetch_assigned
from app.services.activity_query import compute_focus_blocks
from app.services.timezone import day_bounds, is_valid_tz, local_date, now_local, resolve
from app.storage.models import ChatConversation, ChatMessage, Profile
from app.storage.mongodb import activity_events, device_heartbeats, claude_usage, local_commits, ai_tool_events, standups
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import _INJECTION_PATTERNS, sanitize

router = APIRouter()
logger = logging.getLogger(__name__)

def _load_instructions() -> str:
    return llm.load_prompt(
        "instructions.txt",
        "You are a personal work assistant. Answer only from the data provided.",
    )

_MAX_HISTORY_MSGS = 20  # cap conversation context to prevent unbounded token growth

# ponytail: keyword gate (same spirit as the standup gate below) — the live
# Jira fetch is three Atlassian round-trips, too heavy for every chat message.
_JIRA_STATE_WORDS = ("jira", "issue", "ticket", "assigned", "sprint",
                     "overdue", "deadline", "backlog", "story point")


def _format_jira_live(assigned: dict) -> str:
    """Prompt block for the user's CURRENT Jira plate. Unlike ACTIVITY DATA this
    is a live snapshot, independent of the question's date filter."""
    lines = [f"CURRENTLY ASSIGNED JIRA ISSUES (live snapshot, current state — "
             f"independent of the date range above; {len(assigned['issues'])} open):"]
    if assigned.get("done_7d") is not None:
        lines.append(f"  Resolved by the user in the last 7 days: {assigned['done_7d']}")
    for it in assigned["issues"]:
        bits = [it.get("key", ""), it.get("status", ""), it.get("priority", "")]
        if it.get("due_date"):
            bits.append(f"due {it['due_date']}")
        if it.get("sprint"):
            bits.append(it["sprint"])
        if it.get("story_points") is not None:
            bits.append(f"{it['story_points']} pts")
        lines.append("  " + " | ".join(str(b) for b in bits if b)
                     + f" — {sanitize(it.get('summary', ''))}")
    return "\n".join(lines)


def _sanitize_question(text: str) -> str:
    cleaned = _INJECTION_PATTERNS.sub("", text or "")
    return cleaned.strip()[:1000]


# The single-window activity pipeline can't compare two periods, so a token
# comparison ("this week vs last week") gets its own fetch of both periods.
_COMPARE_WORDS = ("last week", "last month", "previous week", "previous month",
                  "compare", "comparison", " vs ", "versus",
                  "week over week", "month over month")


async def _token_comparison_block(profile_id: str, tz_name: str, question: str) -> str:
    ql = question.lower()
    if not any(w in ql for w in _COMPARE_WORDS):
        return ""
    gran = "month" if "month" in ql else "week"
    rng = _period_ranges(gran, now_local(resolve(tz_name)).date())
    (tf, tt), (lf, lt) = rng["this"], rng["last"]
    this_total = await _token_total(profile_id, tf, tt)
    last_total = await _token_total(profile_id, lf, lt)
    if not (this_total or last_total):
        return ""
    delta = this_total - last_total
    pct = f"{delta / last_total * 100:+.0f}%" if last_total else "n/a (no prior data)"
    return (f"CLAUDE TOKEN USAGE COMPARISON (input+output tokens):\n"
            f"  This {gran} ({tf} to {tt}): {this_total:,} tokens\n"
            f"  Last {gran} ({lf} to {lt}): {last_total:,} tokens\n"
            f"  Change: {delta:+,} ({pct})")


def _scope_to_range(scope: str, tz_name: str = "UTC") -> dict:
    tz  = resolve(tz_name)
    now = now_local(tz)
    if scope == "week":
        monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        return {"$gte": day_bounds(monday, tz)[0]}
    # ponytail: month is a rolling window (now - 30d), not day-aligned, so
    # day_bounds doesn't apply. Vestigial — scope is ~always "today".
    if scope == "month":
        return {"$gte": (now - timedelta(days=30)).astimezone(timezone.utc)}
    # today: local midnight → UTC
    return {"$gte": day_bounds(now.strftime("%Y-%m-%d"), tz)[0]}


# Strict schema for structured outputs — nullable enums via ["string","null"] + null
# in the enum; all keys required + additionalProperties:false, as strict mode demands.
_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "date_from":  {"type": ["string", "null"]},
        "date_to":    {"type": ["string", "null"]},
        "source":     {"type": ["string", "null"], "enum": ["github", "gitlab", "jira", "teams", None]},
        "event_type": {"type": ["string", "null"], "enum": ["commit", "pr", "issue", "meeting", "comment", None]},
    },
    "required": ["date_from", "date_to", "source", "event_type"],
}


async def _gpt_parse_intent(question: str, today: str, tz_name: str = "UTC") -> dict:
    """Ask GPT to extract date_from, date_to, source, event_type from the question."""
    local_now    = datetime.now(ZoneInfo(tz_name or "UTC"))
    weekday_name = local_now.strftime("%A")                      # "Tuesday"
    yesterday    = (local_now - timedelta(days=1)).strftime("%Y-%m-%d (%A)")
    system_prompt = (
        f"Today is {weekday_name}, {today} (user's local time). Yesterday was {yesterday}.\n"
        "When the user refers to a day name (e.g. 'Monday', 'last Friday'), resolve it to the "
        "most recent past occurrence of that weekday as a YYYY-MM-DD date.\n"
        "The user will provide a question. Extract the following fields and return ONLY valid JSON — no explanation:\n"
        '  "date_from": YYYY-MM-DD or null\n'
        '  "date_to":   YYYY-MM-DD or null  (null = up to now)\n'
        '  "source":    one of github, gitlab, jira, teams or null\n'
        '  "event_type": one of commit, pr, issue, meeting, comment or null'
    )
    try:
        return await llm.extract_schema(system_prompt, question, _INTENT_SCHEMA, name="intent")
    except Exception as exc:
        logger.warning("GPT intent parse failed: %s", exc)
        return {}


def _intent_to_filter(parsed: dict, scope: str, tz_name: str = "UTC") -> dict:
    """Convert GPT-parsed intent + UI scope into a MongoDB time range filter."""
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")

    if date_from:
        try:
            tz = resolve(tz_name)
            # local-midnight bounds so "yesterday" means the user's actual day, not UTC day
            start, _ = day_bounds(date_from, tz)
            _, end   = day_bounds(date_to or date_from, tz)
            return {"$gte": start, "$lte": end}
        except ValueError:
            pass

    return _scope_to_range(scope, tz_name)


def _period_label(parsed: dict, scope: str) -> str:
    """Human label for the date range the fetched data is already filtered to,
    so the model doesn't mistake filtered data for an unfiltered/cumulative dump."""
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")
    if date_from:
        weekday = datetime.strptime(date_from, "%Y-%m-%d").strftime("%A")
        single  = f"{weekday}, {date_from}"
        return single if (not date_to or date_to == date_from) else f"{single} to {date_to}"
    return {"today": "today", "week": "this week"}.get(scope, scope or "today")


def _map_event_type(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.lower()
    if r in ("pr", "pull_request", "pull request"):
        return "pr_"
    if r == "issue":
        return "issue"
    if r == "comment":
        return "comment"
    return r


def _claude_date_range(time_filter: dict, tzinfo) -> tuple[str, str] | None:
    """Local (date_from, date_to) strings for the claude_usage lookup, which is
    keyed by local date. The filter's upper bound is an EXCLUSIVE next-midnight, so
    step back a second to land on the last real local day — not the day after
    (otherwise a single-day question pulls in the following day's usage). Returns
    None when the filter has no lower bound."""
    start_dt = time_filter.get("$gte")
    if not start_dt:
        return None
    end_dt = time_filter.get("$lte") or time_filter.get("$lt")
    end_dt = (end_dt - timedelta(seconds=1)) if end_dt else datetime.now(timezone.utc)
    return local_date(start_dt, tzinfo), local_date(end_dt, tzinfo)


def _fmt_local(iso_z: str, tz) -> str:
    """'YYYY-MM-DDTHH:MM:SSZ' (naive-UTC) → local clock like '9:00 AM'."""
    dt = datetime.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz)
    return dt.strftime("%I:%M %p").lstrip("0")


async def _fetch_my_activity_context(profile_id: str, time_filter: dict,
                                     tz_name: str = "UTC", question: str = "") -> str:
    lines: list[str] = []

    # Focus time — gap-based blocks, the SAME calc the My Activity page uses
    # (compute_focus_blocks) so the AI answer and the page never disagree.
    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": time_filter, "idle": False},
        projection={"timestamp": 1, "_id": 0},
    ).sort("timestamp", 1).to_list(35_000)
    focus_blocks = compute_focus_blocks(hbs)
    focus_min = sum(b["duration_min"] for b in focus_blocks)
    logger.info(
        "AI context | profile=%s tz=%s filter=%s→%s heartbeats=%d focus_min=%d",
        profile_id[:8], tz_name,
        time_filter.get("$gte"), time_filter.get("$lte") or time_filter.get("$lt"),
        len(hbs), focus_min,
    )
    if focus_min:
        h, m = divmod(focus_min, 60)
        lines.append(f"Focus/coding time: {h}h {m}m (approx)")

    # Claude token usage — keyed by LOCAL date string, derived through the same
    # IANA tz as everything else (docs/adr-0001-timezone.md).
    rng = _claude_date_range(time_filter, resolve(tz_name))
    if rng:
        date_from, date_to = rng
        claude_docs = await claude_usage().find(
            {"profile_id": profile_id, "date": {"$gte": date_from, "$lte": date_to}}
        ).to_list(200)
        logger.info(
            "AI context | claude_usage date=%s→%s found=%d",
            date_from, date_to, len(claude_docs),
        )
        if claude_docs:
            total_in  = sum(d.get("input_tokens",  0) for d in claude_docs)
            total_out = sum(d.get("output_tokens", 0) for d in claude_docs)
            lines.append(f"\nClaude Code usage: {total_in+total_out:,} tokens "
                         f"(input {total_in:,} / output {total_out:,})")
            repos: dict[str, int] = {}
            for d in claude_docs:
                repo = d.get("repo") or "unknown"
                repos[repo] = repos.get(repo, 0) + d.get("input_tokens", 0) + d.get("output_tokens", 0)
            for repo, toks in sorted(repos.items(), key=lambda x: -x[1]):
                lines.append(f"  {repo}: {toks:,} tokens")
            hourly = _merge_hourly(claude_docs)                  # when tokens were spent
            if hourly:
                lines.append("  tokens by hour of day (local):")
                for hb in hourly:
                    h = hb["hour"]; hr = h % 12 or 12; ampm = "am" if h < 12 else "pm"
                    tot = hb["input_tokens"] + hb["output_tokens"]
                    lines.append(f"    {hr}{ampm}: {tot:,} ({hb['input_tokens']:,} in / {hb['output_tokens']:,} out)")

    # Local commits
    commits = await local_commits().find(
        {"profile_id": profile_id, "timestamp": time_filter},
        projection={"repo": 1, "branch": 1, "message": 1, "timestamp": 1, "_id": 0},
    ).sort("timestamp", -1).to_list(50)
    if commits:
        lines.append(f"\nLocal commits: {len(commits)}")
        for c in commits:
            ts  = c.get("timestamp")
            tss = ts.strftime("%Y-%m-%d %H:%M") if ts else ""
            lines.append(f"  [{tss}] {c.get('repo','?')}/{c.get('branch','?')}: "
                         f"{c.get('message','')[:80]}")

    # AI tools — with real active time per tool (running while not idle), the same
    # overlap-with-focus-blocks number the My Activity dropdown shows.
    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": time_filter},
        projection={"tools": 1, "timestamp": 1, "_id": 0},
    ).to_list(2000)
    if ai_docs:
        active_min = _tool_active_minutes(ai_docs, focus_blocks)
        periods    = _tool_active_periods(ai_docs, focus_blocks)
        tz = resolve(tz_name)
        all_tools: set[str] = set()
        for doc in ai_docs:
            all_tools.update(doc.get("tools", []))
        if all_tools:
            lines.append("\nAI tools used (active = running while not idle):")
            for tool in sorted(all_tools):
                mins = active_min.get(tool, 0)
                if mins:
                    h, m = divmod(mins, 60)
                    lines.append(f"  {tool}: {f'{h}h {m}m' if h else f'{m}m'} active")
                else:
                    lines.append(f"  {tool}: detected")
                tool_periods = periods.get(tool, [])
                for p in tool_periods[:8]:                       # sessions = when it was active
                    lines.append(f"    {_fmt_local(p['start'], tz)}–{_fmt_local(p['end'], tz)}")
                if len(tool_periods) > 8:
                    lines.append(f"    (+{len(tool_periods) - 8} more sessions)")

    # Standup history — ONLY when the question is about standups. Dumping 30 days of
    # standup text into every request drowns out sparse activity data and skews every
    # answer toward reciting a standup. ponytail: keyword gate; make it intent-driven
    # if "standup" ever needs synonyms.
    if "standup" in question.lower():
        standup_docs = await standups().find(
            {"profile_id": profile_id},
            projection={"date": 1, "text": 1, "_id": 0},
        ).sort("date", -1).to_list(10)
        if standup_docs:
            lines.append("\nPAST STANDUPS (most recent first):")
            for s in standup_docs:
                lines.append(f"  [{s['date']}] {s['text']}")

    return "\n".join(lines)


# ── Chat conversation endpoints ────────────────────────────────────────────────

@router.get("/api/chat/conversations")
async def list_conversations(profile_id: str = Depends(require_profile)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ChatConversation)
            .where(ChatConversation.profile_id == profile_id)
            .order_by(ChatConversation.updated_at.desc())
        )
        convs = result.scalars().all()
    return JSONResponse([{
        "id": str(c.id),
        "title": c.title,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    } for c in convs])


@router.post("/api/chat/conversations")
async def create_conversation(profile_id: str = Depends(require_profile)):
    async with AsyncSessionLocal() as db:
        conv = ChatConversation(profile_id=profile_id, title="New chat")
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
    return JSONResponse({
        "id": str(conv.id),
        "title": conv.title,
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
    })


@router.get("/api/chat/conversations/{conv_id}/messages")
async def get_conversation_messages(conv_id: str, profile_id: str = Depends(require_profile)):
    async with AsyncSessionLocal() as db:
        conv = (await db.execute(
            select(ChatConversation)
            .where(ChatConversation.id == conv_id, ChatConversation.profile_id == profile_id)
        )).scalar_one_or_none()
        if not conv:
            return JSONResponse({"error": "not_found"}, status_code=404)
        msgs = (await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conv_id)
            .order_by(ChatMessage.created_at.asc())
        )).scalars().all()
    return JSONResponse([{
        "id": str(m.id), "role": m.role,
        "content": m.content, "created_at": m.created_at.isoformat(),
    } for m in msgs])


class AskRequest(BaseModel):
    question: str
    scope: str = "today"
    tz: str | None = None  # browser IANA timezone (ADR-0001); source of truth for local dates


@router.post("/api/chat/conversations/{conv_id}/ask/stream")
async def ask_in_conversation_stream(conv_id: str, body: AskRequest,
                                     profile_id: str = Depends(require_profile)):
    raw_question = body.question.strip()
    if not raw_question:
        return JSONResponse({"error": "question_required"}, status_code=400)
    question = _sanitize_question(raw_question)

    # Fetch conversation + history, timezone, save user message eagerly
    async with AsyncSessionLocal() as db:
        # Browser IANA tz is the freshest signal — resolve local dates from it and
        # persist it so background jobs (summaries, standups) use the same day
        # boundaries the user sees. (docs/adr-0001-timezone.md)
        profile = await db.get(Profile, profile_id)
        profile_tz = (profile.timezone or "UTC") if profile else "UTC"
        if profile and is_valid_tz(body.tz) and body.tz != profile.timezone:
            profile.timezone = body.tz
            profile_tz = body.tz
        tz_name = resolve(profile_tz, body.tz).key

        conv = (await db.execute(
            select(ChatConversation)
            .where(ChatConversation.id == conv_id, ChatConversation.profile_id == profile_id)
        )).scalar_one_or_none()
        if not conv:
            return JSONResponse({"error": "not_found"}, status_code=404)

        history = (await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conv_id)
            .order_by(ChatMessage.created_at.asc())
        )).scalars().all()

        if not history:
            conv.title = raw_question[:80]
        conv_title = conv.title

        user_msg_id     = _uuid.uuid4()
        user_created_at = datetime.now(timezone.utc)
        db.add(ChatMessage(
            id=user_msg_id, conversation_id=conv.id,
            role="user", content=raw_question, created_at=user_created_at,
        ))
        conv.updated_at = user_created_at
        await db.commit()

    # Fetch activity context using user's local timezone
    today  = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    parsed = await _gpt_parse_intent(question, today, tz_name)
    time_filter = _intent_to_filter(parsed, body.scope, tz_name)
    mongo_filter: dict = {"profile_id": profile_id, "occurred_at": time_filter}
    if parsed.get("source"):
        mongo_filter["source"] = parsed["source"]
    et = _map_event_type(parsed.get("event_type"))
    if et:
        mongo_filter["event_type"] = {"$regex": et}
    events = await activity_events().find(mongo_filter).to_list(length=100)
    my_activity = await _fetch_my_activity_context(profile_id, time_filter, tz_name, question)

    jira_live = ""
    q_lower = question.lower()
    if parsed.get("source") == "jira" or any(w in q_lower for w in _JIRA_STATE_WORDS):
        assigned = await fetch_assigned(profile_id)
        if assigned:
            jira_live = _format_jira_live(assigned)

    token_cmp = await _token_comparison_block(profile_id, tz_name, question)

    activity_text = _format_events(events) if events else "No activity data found for that period."
    period_label  = _period_label(parsed, body.scope)
    system_content = (
        f"{_load_instructions()}\n\n"
        f"All data below is already filtered to: {period_label}. Treat it as complete "
        "for that period — do not say it's cumulative or lacks a date breakdown.\n\n"
        f"ACTIVITY DATA:\n{activity_text}"
    )
    if my_activity:
        system_content += f"\n\nDESKTOP/LOCAL ACTIVITY:\n{my_activity}"
    if jira_live:
        system_content += f"\n\n{jira_live}"
    if token_cmp:
        system_content += f"\n\n{token_cmp}"
    chat_history = [
        {"role": m.role, "content": m.content}
        for m in list(history)[-_MAX_HISTORY_MSGS:]
    ]

    async def event_stream():
        tokens: list[str] = []
        try:
            async for delta in llm.answer_stream(
                system_content, question, chat_history,
                max_tokens=400, temperature=0.3,
            ):
                tokens.append(delta)
                yield f"data: {_json.dumps({'token': delta})}\n\n"
        except Exception as exc:
            logger.error("Streaming OpenAI call failed: %s", exc)
            yield f"data: {_json.dumps({'error': str(exc)})}\n\n"
            return

        answer     = "".join(tokens)
        ai_msg_id  = _uuid.uuid4()
        ai_created = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            db.add(ChatMessage(
                id=ai_msg_id, conversation_id=conv_id,
                role="assistant", content=answer, created_at=ai_created,
            ))
            await db.commit()

        yield f"data: {_json.dumps({'done': True, 'ai_message': {'id': str(ai_msg_id), 'role': 'assistant', 'content': answer, 'created_at': ai_created.isoformat()}, 'conversation_title': conv_title})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete("/api/chat/conversations/{conv_id}")
async def delete_conversation(conv_id: str, profile_id: str = Depends(require_profile)):
    async with AsyncSessionLocal() as db:
        conv = (await db.execute(
            select(ChatConversation)
            .where(ChatConversation.id == conv_id, ChatConversation.profile_id == profile_id)
        )).scalar_one_or_none()
        if not conv:
            return JSONResponse({"error": "not_found"}, status_code=404)
        await db.delete(conv)
        await db.commit()
    return JSONResponse({"ok": True})


# ════════════════════════════════════════════════════════════════════════════
# TOOL-CALLING PROTOTYPE (isolated) — POST /api/chat/ask/tools
# The streaming chat above pre-fetches a fixed context + keyword gates. This path
# instead hands the model a few parameterized tools and lets it choose and COMPOSE
# them (call the same tool twice with different periods to compare). Non-streaming,
# no conversation persistence — a sandbox to A/B against the pipeline.
# ════════════════════════════════════════════════════════════════════════════

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
        r = _period_ranges(gran, today)
        if period == f"this_{gran}":
            return r["this"]
        if period == f"last_{gran}":
            return r["last"]
    return _period_ranges("week", today)["this"]        # unknown → this week


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
    active_min = _tool_active_minutes(ai_docs, focus_blocks)
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
