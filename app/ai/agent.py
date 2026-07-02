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
    logger.info("LLM usage | prompt=%d completion=%d total=%d cost=$%.6f",
                pt, ct, pt + ct, _estimate_cost(pt, ct))


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


async def extract_json(system: str, user: str, *, max_tokens: int = 80) -> dict:
    """Structured JSON extraction (temperature 0, json_object mode).

    Raises on invalid JSON — callers decide the fallback.
    """
    resp = await _get_client().chat.completions.create(
        model=settings.AZURE_OPENAI_DEPLOYMENT,
        messages=_messages(system, user),
        max_tokens=max_tokens,
        temperature=0,
        response_format={"type": "json_object"},
    )
    _log_usage(resp.usage)
    return json.loads(resp.choices[0].message.content)


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
