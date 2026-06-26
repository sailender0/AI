"""
Azure AI Foundry agent client — REST API via httpx, api-key auth.

ask_agent()    — persistent chat tied to a ChatConversation row.
run_transient() — one-shot call, no thread persistence (used by summarizer).
"""
import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 1.5
_POLL_TIMEOUT  = 90.0
_API_VERSION   = "2025-05-15-preview"


async def _bearer_token() -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}/oauth2/v2.0/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     settings.AZURE_CLIENT_ID,
                "client_secret": settings.AZURE_CLIENT_SECRET,
                "scope":         "https://management.azure.com/.default",
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def _headers() -> dict:
    token = await _bearer_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _url(path: str) -> str:
    return f"{settings.FOUNDRY_ENDPOINT}/{path}?api-version={_API_VERSION}"


async def _create_thread(client: httpx.AsyncClient) -> str:
    r = await client.post(_url("threads"), headers=await _headers(), json={})
    r.raise_for_status()
    return r.json()["id"]


async def _add_message(client: httpx.AsyncClient, thread_id: str, role: str, content: str) -> None:
    r = await client.post(
        _url(f"threads/{thread_id}/messages"),
        headers=await _headers(),
        json={"role": role, "content": content},
    )
    r.raise_for_status()


async def _run_and_poll(client: httpx.AsyncClient, thread_id: str) -> None:
    r = await client.post(
        _url(f"threads/{thread_id}/runs"),
        headers=await _headers(),
        json={"agent_id": settings.FOUNDRY_AGENT_ID},
    )
    r.raise_for_status()
    run_id = r.json()["id"]

    elapsed = 0.0
    while elapsed < _POLL_TIMEOUT:
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
        status_r = await client.get(_url(f"threads/{thread_id}/runs/{run_id}"), headers=await _headers())
        status = status_r.json().get("status", "")
        if status == "completed":
            return
        if status in ("failed", "cancelled", "expired"):
            raise RuntimeError(f"Foundry run ended with status: {status}")
    raise TimeoutError("Foundry run timed out")


async def _last_message(client: httpx.AsyncClient, thread_id: str) -> str:
    r = await client.get(
        _url(f"threads/{thread_id}/messages"),
        headers=await _headers(),
        params={"order": "desc", "limit": 1},
    )
    r.raise_for_status()
    messages = r.json().get("data", [])
    for msg in messages:
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                return block["text"]["value"]
    return ""


async def ask_agent(
    question: str,
    activity_context: str,
    conversation,
    history: list[dict],
    db,
) -> tuple[str, str]:
    """Persistent chat — reuses Foundry thread, recovers if expired."""
    async with httpx.AsyncClient(timeout=120) as client:
        thread_id = conversation.foundry_thread_id

        if thread_id:
            try:
                r = await client.get(_url(f"threads/{thread_id}"), headers=await _headers())
                if r.status_code == 404:
                    thread_id = None
            except Exception:
                thread_id = None

        if not thread_id:
            thread_id = await _create_thread(client)
            for msg in history[-20:]:
                role = "user" if msg["role"] == "user" else "assistant"
                await _add_message(client, thread_id, role, msg["content"])

        user_content = f"ACTIVITY DATA:\n{activity_context}\n\nUser question: {question}"
        await _add_message(client, thread_id, "user", user_content)
        await _run_and_poll(client, thread_id)
        answer = await _last_message(client, thread_id)

    return answer, thread_id


async def run_transient(prompt: str) -> str:
    """One-shot agent call — no thread persistence."""
    async with httpx.AsyncClient(timeout=120) as client:
        thread_id = await _create_thread(client)
        await _add_message(client, thread_id, "user", prompt)
        await _run_and_poll(client, thread_id)
        return await _last_message(client, thread_id)
