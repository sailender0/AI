"""
On-demand Q&A endpoint + persistent multi-turn chat conversations.
"""
import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.ai.summarizer import _format_events, _openai_client
from app.auth.sso import get_profile_from_session
from app.config import settings
from app.storage.models import QueryLog, ChatConversation, ChatMessage
from app.storage.mongodb import activity_events
from app.storage.postgres import AsyncSessionLocal
from app.webhooks.normalizer import _INJECTION_PATTERNS

router = APIRouter()
logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):
    question: str
    scope: str = "today"


def _sanitize_question(text: str) -> str:
    cleaned = _INJECTION_PATTERNS.sub("", text or "")
    return cleaned.strip()[:1000]


def _scope_to_range(scope: str) -> dict:
    now = datetime.now(timezone.utc)
    if scope == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return {"$gte": start}
    if scope == "month":
        return {"$gte": now - timedelta(days=30)}
    if scope == "all":
        return {"$gte": now - timedelta(days=365)}
    # default: today
    return {"$gte": now.replace(hour=0, minute=0, second=0, microsecond=0)}


async def _gpt_parse_intent(question: str, client, today: str) -> dict:
    """Ask GPT to extract date_from, date_to, source, event_type from the question."""
    prompt = (
        f"Today's date is {today} (UTC). "
        "Extract the following from the user's question and return ONLY valid JSON — no explanation:\n"
        '  "date_from": YYYY-MM-DD or null\n'
        '  "date_to":   YYYY-MM-DD or null  (null = up to now)\n'
        '  "source":    one of github, gitlab, jira, teams or null\n'
        '  "event_type": one of commit, pr, issue, meeting, comment or null\n\n'
        f'Question: "{question}"'
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.AZURE_OPENAI_DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0,
            response_format={"type": "json_object"},
        )
        import json as _json
        return _json.loads(resp.choices[0].message.content)
    except Exception as exc:
        logger.warning("GPT intent parse failed: %s", exc)
        return {}


def _intent_to_filter(parsed: dict, scope: str) -> dict:
    """Convert GPT-parsed intent + UI scope into a MongoDB time range filter."""
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")

    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            time_filter: dict = {"$gte": df}
            if date_to:
                dt = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
                time_filter["$lte"] = dt
            else:
                # single-day query when date_from == date_to or only from given
                if not date_to:
                    time_filter["$lte"] = df + timedelta(days=1)
            return time_filter
        except ValueError:
            pass

    # GPT found no date — fall back to UI scope
    return _scope_to_range(scope)


def _map_event_type(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.lower()
    if r in ("pr", "pull_request", "pull request"):
        return "pr_"
    if r == "issue":
        return "issue_updated"
    return r


def _build_query_prompt(question: str, events: list[dict]) -> str:
    return (
        "You are a personal work assistant answering a question about "
        "a developer's own activity. Answer only from the data below.\n"
        "Be concise. If the answer is a count, state the number first.\n"
        "Do not guess or invent anything not in the data.\n\n"
        f"User question: {question}\n\n"
        "ACTIVITY DATA START\n"
        f"{_format_events(events)}\n"
        "ACTIVITY DATA END"
    )


@router.post("/query")
async def query(request: Request, body: QueryRequest):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    raw_question = body.question.strip()
    if not raw_question:
        return JSONResponse({"error": "question_required"}, status_code=400)

    question = _sanitize_question(raw_question)

    client   = _openai_client()
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parsed   = await _gpt_parse_intent(question, client, today)

    mongo_filter: dict = {"profile_id": profile_id, "occurred_at": _intent_to_filter(parsed, body.scope)}
    if parsed.get("source"):
        mongo_filter["source"] = parsed["source"]
    et = _map_event_type(parsed.get("event_type"))
    if et:
        mongo_filter["event_type"] = {"$regex": f"^{et}"} if et.endswith("_") else et

    events = await activity_events().find(mongo_filter).to_list(length=100)

    if not events:
        return JSONResponse({"answer": "No activity found for that filter."})

    prompt = _build_query_prompt(question, events)

    client = _openai_client()
    try:
        response = await client.chat.completions.create(
            model=settings.AZURE_OPENAI_DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.2,
        )
    except Exception as exc:
        logger.error("OpenAI call failed: %s", exc)
        return JSONResponse(
            {"error": f"AI call failed: {exc}"},
            status_code=502,
        )
    answer = response.choices[0].message.content.strip()

    if response.usage:
        u = response.usage
        logger.info("Query tokens — in=%d out=%d total=%d  cost=$%.5f",
            u.prompt_tokens, u.completion_tokens, u.total_tokens,
            u.prompt_tokens / 1_000_000 * 2.50 + u.completion_tokens / 1_000_000 * 10.00)

    async with AsyncSessionLocal() as db:
        log = QueryLog(
            profile_id=profile_id,
            question=raw_question,
            filters_json={
                "source": parsed.get("source"),
                "event_type": parsed.get("event_type"),
                "date_from": parsed.get("date_from"),
                "date_to": parsed.get("date_to"),
                "scope": body.scope,
            },
            ai_response=answer,
            context_event_ids=[str(e.get("_id")) for e in events],
        )
        db.add(log)
        await db.commit()

    return JSONResponse({"answer": answer})


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


@router.post("/api/chat/conversations/{conv_id}/ask")
async def ask_in_conversation(request: Request, conv_id: str, body: AskRequest):
    profile_id = await get_profile_from_session(request)
    if not profile_id:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    raw_question = body.question.strip()
    if not raw_question:
        return JSONResponse({"error": "question_required"}, status_code=400)
    question = _sanitize_question(raw_question)

    async with AsyncSessionLocal() as db:
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

        # Set title from first question
        if not history:
            conv.title = raw_question[:80]

        # Save user message (generate id in Python so we have it before flush)
        user_msg_id = _uuid.uuid4()
        user_created_at = datetime.now(timezone.utc)
        user_msg = ChatMessage(
            id=user_msg_id, conversation_id=conv.id,
            role="user", content=raw_question, created_at=user_created_at,
        )
        db.add(user_msg)
        conv.updated_at = datetime.now(timezone.utc)

        # Fetch activity data for latest question
        client2  = _openai_client()
        today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        parsed   = await _gpt_parse_intent(question, client2, today)
        mongo_filter: dict = {"profile_id": profile_id, "occurred_at": _intent_to_filter(parsed, body.scope)}
        if parsed.get("source"):
            mongo_filter["source"] = parsed["source"]
        et = _map_event_type(parsed.get("event_type"))
        if et:
            mongo_filter["event_type"] = {"$regex": f"^{et}"} if et.endswith("_") else et
        events = await activity_events().find(mongo_filter).to_list(length=100)

        activity_text = _format_events(events) if events else "No activity data found for that period."
        system_content = (
            "You are a personal work assistant for a software developer. "
            "Answer questions about their activity using only the data provided. "
            "Be concise. If the answer is a count, state it first. "
            "Do not invent anything not in the data.\n\n"
            f"ACTIVITY DATA:\n{activity_text}"
        )

        openai_messages = [{"role": "system", "content": system_content}]
        for m in history:
            openai_messages.append({"role": m.role, "content": m.content})
        openai_messages.append({"role": "user", "content": question})

        client = _openai_client()
        try:
            response = await client.chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=openai_messages,
                max_tokens=400,
                temperature=0.3,
            )
        except Exception as exc:
            logger.error("OpenAI call failed: %s", exc)
            await db.rollback()
            return JSONResponse({"error": f"AI call failed: {exc}"}, status_code=502)

        answer = response.choices[0].message.content.strip()
        if response.usage:
            u = response.usage
            logger.info("Chat tokens — conv=%s in=%d out=%d total=%d  cost=$%.5f",
                conv_id, u.prompt_tokens, u.completion_tokens, u.total_tokens,
                u.prompt_tokens / 1_000_000 * 2.50 + u.completion_tokens / 1_000_000 * 10.00)
        ai_msg_id = _uuid.uuid4()
        ai_created_at = datetime.now(timezone.utc)
        ai_msg = ChatMessage(
            id=ai_msg_id, conversation_id=conv.id,
            role="assistant", content=answer, created_at=ai_created_at,
        )
        db.add(ai_msg)
        await db.commit()

    return JSONResponse({
        "user_message":   {"id": str(user_msg_id), "role": "user", "content": raw_question, "created_at": user_created_at.isoformat()},
        "ai_message":     {"id": str(ai_msg_id), "role": "assistant", "content": answer, "created_at": ai_created_at.isoformat()},
        "conversation_title": conv.title,
    })


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
