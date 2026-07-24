"""
Persistent multi-turn chat: conversation CRUD, the streaming Q&A endpoint, and
emailing an answer to the user. The streaming path pre-fetches a fixed context
(via context.py) + keyword gates before calling the model.
"""
import logging
import uuid as _uuid
import json as _json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.ai import llm
from app.ai.context import (
    _JIRA_STATE_WORDS, _chart_link, _fetch_my_activity_context, _format_jira_live,
    _gpt_parse_intent, _intent_to_filter, _load_instructions, _map_event_type,
    _period_label, _sanitize_question, _token_comparison_block,
)
from app.ai.summarizer import _format_events
from app.auth.rbac import require_permission
from app.auth.sso import require_profile
from app.delivery.email_delivery import send_mail
from app.services.email_report import render_chat
from app.services.jira_board import fetch_assigned
from app.services.timezone import is_valid_tz, resolve
from app.storage.models import ChatConversation, ChatMessage, Profile
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_HISTORY_MSGS = 20  # cap conversation context to prevent unbounded token growth


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

        yield f"data: {_json.dumps({'done': True, 'ai_message': {'id': str(ai_msg_id), 'role': 'assistant', 'content': answer, 'created_at': ai_created.isoformat()}, 'conversation_title': conv_title, 'chart_link': _chart_link(question)})}\n\n"

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


class EmailMsgRequest(BaseModel):
    message_id: str | None = None   # which assistant message to email; None = most recent


async def _assistant_message(db, conv_id: str, profile_id: str, message_id: str | None):
    """(conv, msg) for an assistant message in the user's conversation. `message_id`
    selects a specific one (scoped to the conversation, so no cross-conv leakage);
    None picks the most recent. Either may be None if not found."""
    conv = (await db.execute(
        select(ChatConversation)
        .where(ChatConversation.id == conv_id, ChatConversation.profile_id == profile_id)
    )).scalar_one_or_none()
    if not conv:
        return None, None
    q = select(ChatMessage).where(
        ChatMessage.conversation_id == conv_id, ChatMessage.role == "assistant")
    if message_id:
        msg = (await db.execute(q.where(ChatMessage.id == message_id))).scalars().first()
    else:
        msg = (await db.execute(q.order_by(ChatMessage.created_at.desc()))).scalars().first()
    return conv, msg


@router.post("/api/chat/conversations/{conv_id}/email/preview")
async def preview_email_answer(conv_id: str, body: EmailMsgRequest,
                              profile_id: str = Depends(require_permission("email_ai_answer"))):
    """Report-styled HTML for a chosen (or latest) AI answer — no send."""
    async with AsyncSessionLocal() as db:
        conv, msg = await _assistant_message(db, conv_id, profile_id, body.message_id)
    if not conv:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not msg:
        return JSONResponse({"error": "nothing_to_send"}, status_code=400)
    subject, html_body = render_chat(conv.title or "AI answer", msg.content)
    return JSONResponse({"subject": subject, "html": html_body})


@router.post("/api/chat/conversations/{conv_id}/email")
async def email_answer(conv_id: str, body: EmailMsgRequest,
                       profile_id: str = Depends(require_permission("email_ai_answer"))):
    """Email a chosen (or latest) AI answer to the user (self-only). Re-renders from
    the stored message — no client-supplied HTML."""
    async with AsyncSessionLocal() as db:
        conv, msg = await _assistant_message(db, conv_id, profile_id, body.message_id)
        profile = await db.get(Profile, profile_id) if conv else None
    if not conv:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not profile or not profile.email:
        return JSONResponse({"error": "no_email"}, status_code=400)
    if not msg:
        return JSONResponse({"error": "nothing_to_send"}, status_code=400)

    subject, html_body = render_chat(conv.title or "AI answer", msg.content)
    sent = await send_mail(profile_id, profile.email, subject, html_body)
    return JSONResponse({"sent": sent})
