# Swapping the LLM provider

**Last updated:** July 30, 2026

How to move off Azure OpenAI — to xAI Grok, Anthropic Claude, Gemini, a local
model, whatever comes next.

---

## The one rule

`app/ai/llm.py` is the **only** file in the repo that imports `openai`. Every
caller (`chat.py`, `summarizer.py`, `insights.py`, `tools.py`, the standup job)
passes plain strings — system prompt, user text, optional history — and gets a
string back. Provider swaps happen inside that file and nowhere else.

If a swap starts pulling you into route or summarizer code, stop: something has
leaked out of the seam and that leak is the bug to fix first.

---

## Two kinds of swap

### A. OpenAI-wire-compatible — config change, ~8 lines

Covers **xAI Grok**, Gemini (OpenAI-compat endpoint), DeepSeek, Groq, Mistral,
GitHub Models, local Ollama / vLLM, and Anthropic's own compat endpoint. Note
that "GitHub Copilot" is not an option here — Copilot has no general-purpose app
API; GitHub Models is the programmatic equivalent.

Nothing about the message envelope, tool schema, or exception handling changes.
The `openai` SDK stays in `requirements.txt`.

| Where | Change |
|---|---|
| `llm.py` import | `from openai import AsyncOpenAI` (drop `AsyncAzureOpenAI`) |
| `llm.py` `_client` / `_get_client()` | annotation → `AsyncOpenAI` |
| `llm.py` `_get_client()` body | `AsyncOpenAI(base_url=…, api_key=…, max_retries=3)` — `azure_endpoint` becomes `base_url`, and **delete `api_version`** (Azure-only) |
| `llm.py` ×4 `model=` call sites | value is now a real model id, not an Azure *deployment* name — edit only if you rename the setting |
| `app/config.py` | endpoint / key / deployment / the two price floats |
| `.env`, `.env.example` | same five values |
| `tests/test_cost.py` | only if you rename the settings |
| `docs/overview.md` | the stack table row and the token-cost note |

### B. Native non-OpenAI SDK — rewrite of four function bodies, ~80 lines

Only needed for provider-specific features the compat endpoint doesn't expose
(Anthropic prompt caching, extended thinking, 1M context). Otherwise take route A.

What actually differs on the Anthropic wire:

- `system` is a top-level parameter, not a message in the list
- tools are `{name, input_schema}`, not `{type:"function", function:{…}}`
- tool results are content blocks, not `{"role": "tool"}` messages
- `max_tokens` is required
- no `response_format` / `json_schema` — structured extraction is done by
  forcing a tool call instead

Translate all of it **inside `llm.py`**. `_TOOLS` in `app/ai/tools.py` stays in
OpenAI shape; a 5-line converter in `llm.py` is a smaller diff than rewriting
the tool definitions and it keeps route A working.

---

## Worked example — Azure OpenAI → xAI Grok

Route A. `base_url = "https://api.x.ai/v1"`, api key from the xAI console,
model id e.g. `grok-4-fast`. Set `*_PRICE_IN` / `*_PRICE_OUT` from xAI's pricing
page for that exact model — leaving the `gpt-4.1-mini` defaults (0.40 / 1.60)
makes every logged cost silently wrong.

---

## Verify after any swap

Three things Azure OpenAI provides that a compat endpoint may not. All three
degrade rather than crash, which is exactly why they need checking by hand:

1. **Structured outputs.** `extract_schema()` tries strict `json_schema` first
   and falls back to `json_object`, logging `json_schema rejected by deployment`.
   That warning is not an error but it means less reliable extraction — watch the
   log after a swap.
2. **Streaming usage.** `stream_options={"include_usage": True}`. If unsupported,
   streaming still works; you just silently lose cost logging on streamed replies.
3. **Cached-prompt tokens.** `usage.prompt_tokens_details.cached_tokens` is
   `getattr`-guarded and degrades to `cached=0`.

Then exercise, in order: `/ai` streaming chat, `POST /api/chat/ask/tools`
(tool-calling — the most provider-sensitive path), a manual daily summary, and
the agent insights bubble.

---

## Don't

- Add a `BaseLLM` ABC, a provider registry, or a factory. One `if` in
  `_get_client()` covers every compat provider; an interface with one live
  implementation is pure overhead.
- Add LiteLLM or a similar router. It does what the `if` does, plus ~40
  transitive dependencies.
- Put provider config in the database or expose per-request model choice in the
  UI. Cost, prompt tuning, and structured-output behaviour are all per-model —
  one model at a time, chosen by env, is the whole requirement.

## Unrelated, despite the name

`SECRET_KEY` keys the Fernet encryption of the OAuth **token store**
(`app/auth/token_store.py`), not the LLM. A provider swap does not touch it.
