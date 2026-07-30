"""The single seam between the app and the LLM backend.

Every LLM call in the app goes through here. Callers pass plain strings
(system prompt, user content, optional history); this module owns the client,
the model name, the message envelope, and response extraction.

To swap backend (Azure OpenAI <-> Foundry agent) change the bodies below —
no route, summarizer, or standup code needs to change.
"""
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import openai
from openai import AsyncAzureOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent


def load_prompt(filename: str, fallback: str = "") -> str:
    """Load a prompt file from app/ai/, dropping #-comment lines.

    Files with no #-lines are returned verbatim (stripped). Missing file -> fallback.
    """
    try:
        text = (_PROMPT_DIR / filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        return fallback
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#")).strip()


_client: AsyncAzureOpenAI | None = None


def _get_client() -> AsyncAzureOpenAI:
    global _client
    if not settings.AI_ENABLED:
        # Message reaches the UI verbatim (routes surface str(exc)) — keep it user-facing.
        raise RuntimeError("Currently all AI features are disabled")
    if _client is None:
        _client = AsyncAzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_KEY,
            api_version="2024-08-01-preview",
            max_retries=3,
        )
    return _client


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost from token counts, using the per-1M prices in config. Those
    prices must be updated alongside AZURE_OPENAI_DEPLOYMENT when the model
    changes (see config.py)."""
    return (prompt_tokens * settings.AZURE_OPENAI_PRICE_IN
            + completion_tokens * settings.AZURE_OPENAI_PRICE_OUT) / 1_000_000


def _log_usage(usage) -> None:
    if not usage:
        return
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    cached = 0                                    # cached prefix tokens (~0.5x price)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    logger.info("LLM usage | prompt=%d (cached=%d) completion=%d total=%d cost=$%.6f",
                pt, cached, ct, pt + ct, _estimate_cost(pt, ct))


def _messages(system: str, user: str, history: list[dict] | None = None) -> list[dict]:
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in history or []:
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": user})
    return msgs


async def answer(
    system: str,
    user: str,
    history: list[dict] | None = None,
    *,
    max_tokens: int,
    temperature: float,
) -> str:
    """One-shot completion. Returns the stripped answer text."""
    resp = await _get_client().chat.completions.create(
        model=settings.AZURE_OPENAI_DEPLOYMENT,
        messages=_messages(system, user, history),
        max_tokens=max_tokens,
        temperature=temperature,
    )
    _log_usage(resp.usage)
    return resp.choices[0].message.content.strip()


async def answer_stream(
    system: str,
    user: str,
    history: list[dict] | None = None,
    *,
    max_tokens: int,
    temperature: float,
) -> AsyncIterator[str]:
    """Streamed completion. Yields content deltas as they arrive."""
    stream = await _get_client().chat.completions.create(
        model=settings.AZURE_OPENAI_DEPLOYMENT,
        messages=_messages(system, user, history),
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
        stream_options={"include_usage": True},
    )
    async for chunk in stream:
        if getattr(chunk, "usage", None):
            _log_usage(chunk.usage)          # final usage-only chunk (choices == [])
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


async def answer_with_tools(
    system: str,
    user: str,
    tools: list[dict],
    dispatch,
    history: list[dict] | None = None,
    *,
    max_tokens: int,
    temperature: float,
    max_rounds: int = 4,
) -> str:
    """Agentic completion: the model may call `tools`; each call is run via
    `dispatch(name, args)` (async, returns a JSON-able result) and fed back until
    the model returns text. Bounded by max_rounds so a tool loop can't run away."""
    msgs = _messages(system, user, history)
    for _ in range(max_rounds):
        resp = await _get_client().chat.completions.create(
            model=settings.AZURE_OPENAI_DEPLOYMENT,
            messages=msgs,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        _log_usage(resp.usage)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return (msg.content or "").strip()
        msgs.append(msg)                                    # the assistant's tool-call request
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await dispatch(tc.function.name, args)
            msgs.append({"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(result, default=str)})
    return "Sorry — I couldn't finish that lookup in time."


async def extract_schema(system: str, user: str, schema: dict, *,
                         name: str = "result", max_tokens: int = 120) -> dict:
    """Structured JSON extraction constrained to `schema` (strict structured outputs,
    temperature 0). If the deployment/api-version rejects `json_schema`, falls back to
    plain `json_object` mode so a non-structured-outputs model degrades instead of
    erroring. Returns {} on a refusal / empty content. Raises on invalid JSON —
    callers decide the fallback.
    ponytail: the json_object fallback keeps Ask AI working even where structured
    outputs isn't available; drop it once every deployment is confirmed to support it."""
    strict = {"type": "json_schema",
              "json_schema": {"name": name, "strict": True, "schema": schema}}
    for response_format in (strict, {"type": "json_object"}):
        try:
            resp = await _get_client().chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=_messages(system, user),
                max_tokens=max_tokens,
                temperature=0,
                response_format=response_format,
            )
        except openai.BadRequestError:
            if response_format is strict:
                logger.warning("json_schema rejected by deployment — falling back to json_object")
                continue
            raise
        _log_usage(resp.usage)
        content = resp.choices[0].message.content
        return json.loads(content) if content else {}   # content is None on a refusal
    return {}


def demo() -> None:
    """Self-check: envelope assembly is the only pure logic here."""
    assert _messages("", "hi") == [{"role": "user", "content": "hi"}]
    assert _messages("sys", "hi") == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert _messages("sys", "q", [{"role": "user", "content": "a"},
                                  {"role": "assistant", "content": "b"}]) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "q"},
    ]
    print("ok")


if __name__ == "__main__":
    demo()
