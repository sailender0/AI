"""
On-demand Q&A endpoint + persistent multi-turn chat conversations.
"""
import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import json as _json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.ai import agent
from app.ai.summarizer import _format_events
from app.auth.sso import get_profile_from_session
from app.services.activity_query import compute_focus_blocks
from app.services.timezone import day_bounds, is_valid_tz, local_date, now_local, resolve
from app.storage.models import ChatConversation, ChatMessage, Profile
from app.storage.mongodb import activity_events, device_heartbeats, claude_usage, local_commits, ai_tool_events, standups
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import _INJECTION_PATTERNS

router = APIRouter()
logger = logging.getLogger(__name__)

def _load_instructions() -> str:
    return agent.load_prompt(
        "instructions.txt",
        "You are a personal work assistant. Answer only from the data provided.",
    )

_MAX_HISTORY_MSGS = 20  # cap conversation context to prevent unbounded token growth


def _sanitize_question(text: str) -> str:
    cleaned = _INJECTION_PATTERNS.sub("", text or "")
    return cleaned.strip()[:1000]


def _scope_to_range(scope: str, tz_name: str = "UTC") -> dict:
    tz  = resolve(tz_name)
    now = now_local(tz)
    if scope == "week":
        monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        return {"$gte": day_bounds(monday, tz)[0]}
    # ponytail: month/all are rolling windows (now - N days), not day-aligned,
    # so day_bounds doesn't apply. Both are vestigial — scope is ~always "today".
    if scope == "month":
        return {"$gte": (now - timedelta(days=30)).astimezone(timezone.utc)}
    if scope == "all":
        return {"$gte": (now - timedelta(days=365)).astimezone(timezone.utc)}
    # today: local midnight → UTC
    return {"$gte": day_bounds(now.strftime("%Y-%m-%d"), tz)[0]}


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
        return await agent.extract_json(system_prompt, question, max_tokens=80)
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


async def _fetch_my_activity_context(profile_id: str, time_filter: dict, tz_name: str = "UTC") -> str:
    lines: list[str] = []

    # Focus time — gap-based blocks, the SAME calc the My Activity page uses
    # (compute_focus_blocks) so the AI answer and the page never disagree.
    hbs = await device_heartbeats().find(
        {"profile_id": profile_id, "timestamp": time_filter, "idle": False},
        projection={"timestamp": 1, "_id": 0},
    ).sort("timestamp", 1).to_list(35_000)
    focus_min = sum(b["duration_min"] for b in compute_focus_blocks(hbs))
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

    # AI tools detected
    ai_docs = await ai_tool_events().find(
        {"profile_id": profile_id, "timestamp": time_filter},
        projection={"tools": 1, "_id": 0},
    ).to_list(2000)
    if ai_docs:
        all_tools: set[str] = set()
        for doc in ai_docs:
            all_tools.update(doc.get("tools", []))
        if all_tools:
            lines.append(f"\nAI tools detected: {', '.join(sorted(all_tools))}")

    # Standup history — last 30 days so user can ask "what was my standup last Tuesday?"
    standup_docs = await standups().find(
        {"profile_id": profile_id},
        projection={"date": 1, "text": 1, "_id": 0},
    ).sort("date", -1).to_list(30)
    if standup_docs:
        lines.append("\nPAST STANDUPS (most recent first):")
        for s in standup_docs:
            lines.append(f"  [{s['date']}] {s['text']}")

    return "\n".join(lines)


# ── Chat conversation endpoints ────────────────────────────────────────────────

@router.get("/api/chat/conversations")
async def list_conversations(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
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
async def create_conversation(request: Request):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
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
async def get_conversation_messages(request: Request, conv_id: str):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
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
async def ask_in_conversation_stream(request: Request, conv_id: str, body: AskRequest):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

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
    my_activity = await _fetch_my_activity_context(profile_id, time_filter, tz_name)

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
    chat_history = [
        {"role": m.role, "content": m.content}
        for m in list(history)[-_MAX_HISTORY_MSGS:]
    ]

    async def event_stream():
        tokens: list[str] = []
        try:
            async for delta in agent.answer_stream(
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
async def delete_conversation(request: Request, conv_id: str):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
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
