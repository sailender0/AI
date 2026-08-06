"""Shared schemas and device auth dependency for agent routes."""
import hashlib
from datetime import datetime

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.storage.models import Device, DeviceToken
from app.storage.postgres import AsyncSessionLocal


async def _get_device(request: Request) -> tuple[Device, str]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing device token")
    token_hash = hashlib.sha256(auth[7:].encode()).hexdigest()
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(DeviceToken).where(DeviceToken.token_hash == token_hash)
        )).scalar_one_or_none()
        if not row:
            raise HTTPException(401, "Invalid device token")
        device = (await db.execute(
            select(Device).where(Device.id == row.device_id)
        )).scalar_one()
    return device, str(device.profile_id)


DeviceCtx = tuple[Device, str]


class HeartbeatPayload(BaseModel):
    active_app:   str = ""
    window_title: str = ""
    git_repo:     str | None = None
    git_branch:   str | None = None
    idle:         bool = False
    timestamp:    datetime | None = None


class CommitPayload(BaseModel):
    repo:          str
    branch:        str
    sha:           str
    message:       str = Field(max_length=500)
    files_changed: int = 0
    insertions:    int = 0
    deletions:     int = 0
    timestamp:     datetime | None = None


class AiEventPayload(BaseModel):
    tools:     list[str]
    timestamp: datetime | None = None


class HourlyBucket(BaseModel):
    hour:          int = 0
    input_tokens:  int = 0
    output_tokens: int = 0


class ClaudeUsageEntry(BaseModel):
    date:                  str
    model:                 str
    repo:                  str = ""
    input_tokens:          int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens:     int = 0
    output_tokens:         int = 0
    message_count:         int = 0
    files:                 list[str] = []
    hourly:                list[HourlyBucket] = []


class ClaudeUsagePayload(BaseModel):
    entries: list[ClaudeUsageEntry]


class VscodeExtensionsPayload(BaseModel):
    extensions: list[str]
