"""
Public facade for the AI HTTP surface. The implementation is split across:
  - context.py    shared data-fetch / prompt-building helpers (no routes)
  - chat.py       persistent chat conversations + email-an-answer
  - tools.py      tool-calling prototype (/api/chat/ask/tools)
  - insights.py   proactive agent bubble (/api/agent/insights)

This module mounts every sub-router under one `router` (what app.main includes)
and re-exports the helpers that tests import from `app.ai.query`.
"""
from fastapi import APIRouter

from app.ai import chat, insights, tools

# Re-exports so existing `from app.ai.query import X` imports (tests + main) keep
# working. Only the names actually imported elsewhere — add one back when a caller
# needs it; endpoint handlers reach the app via `router`, not by name.
from app.ai.context import (  # noqa: F401
    _claude_date_range, _format_jira_live, _intent_to_filter, _map_event_type,
    _period_label, _sanitize_question, _scope_to_range,
)
from app.ai.chat import get_conversation_messages          # noqa: F401
from app.ai.tools import _resolve_period                    # noqa: F401
from app.ai.insights import _jira_due_buckets, _keys_phrase  # noqa: F401

router = APIRouter()
router.include_router(chat.router)
router.include_router(tools.router)
router.include_router(insights.router)
