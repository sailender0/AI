"""Receive data from the desktop agent."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pymongo.errors import DuplicateKeyError

from app.middleware.rate_limit import limiter
from app.storage.models import Device
from app.storage.mongodb import ai_tool_events, claude_usage, device_heartbeats, local_commits, vscode_extensions
from app.storage.postgres import AsyncSessionLocal

from ._base import (
    AiEventPayload, ClaudeUsagePayload, CommitPayload,
    HeartbeatPayload, VscodeExtensionsPayload, _get_device,
)

router = APIRouter()


@router.post("/heartbeat")
@limiter.limit("6/minute")
async def receive_heartbeat(
    request: Request,
    body: HeartbeatPayload,
    ctx: tuple = Depends(_get_device),
):
    device, profile_id = ctx
    now = datetime.now(timezone.utc)
    await device_heartbeats().insert_one({
        "profile_id":   profile_id,
        "device_id":    str(device.id),
        "active_app":   body.active_app[:100],
        "git_repo":     body.git_repo,
        "git_branch":   body.git_branch,
        "idle":         body.idle,
        "timestamp":    now,
    })
    async with AsyncSessionLocal() as db:
        d = await db.get(Device, device.id)
        if d:
            d.last_seen = now
            await db.commit()
    return {"ok": True}


@router.post("/commit")
@limiter.limit("60/minute")
async def receive_commit(
    request: Request,
    body: CommitPayload,
    ctx: tuple = Depends(_get_device),
):
    device, profile_id = ctx
    doc = {
        "profile_id":    profile_id,
        "device_id":     str(device.id),
        "repo":          body.repo,
        "branch":        body.branch,
        "sha":           body.sha[:12],
        "message":       body.message,
        "files_changed": body.files_changed,
        "insertions":    body.insertions,
        "deletions":     body.deletions,
        "timestamp":     body.timestamp or datetime.now(timezone.utc),
    }
    try:
        await local_commits().insert_one(doc)
    except DuplicateKeyError:
        pass
    return {"ok": True}


@router.post("/ai-event")
@limiter.limit("6/minute")
async def receive_ai_event(
    request: Request,
    body: AiEventPayload,
    ctx: tuple = Depends(_get_device),
):
    device, profile_id = ctx
    await ai_tool_events().insert_one({
        "profile_id": profile_id,
        "device_id":  str(device.id),
        "tools":      body.tools,
        "timestamp":  body.timestamp or datetime.now(timezone.utc),
    })
    return {"ok": True}


@router.post("/claude-usage")
@limiter.limit("12/minute")
async def receive_claude_usage(
    request: Request,
    body: ClaudeUsagePayload,
    ctx: tuple = Depends(_get_device),
):
    device, profile_id = ctx
    col = claude_usage()
    for entry in body.entries:
        update: dict = {
            "$inc": {
                "input_tokens":          entry.input_tokens,
                "cache_creation_tokens": entry.cache_creation_tokens,
                "cache_read_tokens":     entry.cache_read_tokens,
                "output_tokens":         entry.output_tokens,
                "message_count":         entry.message_count,
            },
            "$set": {"device_id": str(device.id)},
        }
        if entry.files:
            update["$addToSet"] = {"files": {"$each": entry.files}}
        await col.update_one(
            {"profile_id": profile_id, "date": entry.date,
             "model": entry.model, "repo": entry.repo},
            update,
            upsert=True,
        )
    return {"ok": True}


@router.post("/vscode-extensions")
@limiter.limit("6/minute")
async def receive_vscode_extensions(
    request: Request,
    body: VscodeExtensionsPayload,
    ctx: tuple = Depends(_get_device),
):
    device, profile_id = ctx
    await vscode_extensions().update_one(
        {"profile_id": profile_id, "device_id": str(device.id)},
        {"$set": {
            "extensions": body.extensions,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return {"ok": True}
