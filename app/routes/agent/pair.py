"""Device registration and management."""
import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.auth.sso import require_profile
from app.middleware.rate_limit import limiter
from app.storage.models import Device, DeviceToken
from app.storage.postgres import AsyncSessionLocal

router = APIRouter()

_MAX_DEVICES = 10


class RegisterBody(BaseModel):
    device_name: str
    platform:    str = "windows"


@router.post("/register")
@limiter.limit("5/minute")
async def register_device(body: RegisterBody, request: Request,
                          profile_id: str = Depends(require_profile)):
    """Called by desktop app after SSO callback — creates device + token."""
    raw_token  = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    async with AsyncSessionLocal() as db:
        count = (await db.execute(
            select(func.count()).select_from(Device)
            .where(Device.profile_id == profile_id)
        )).scalar_one()
        if count >= _MAX_DEVICES:
            raise HTTPException(429, f"Device limit reached ({_MAX_DEVICES})")

        device = Device(
            profile_id=profile_id,
            name=body.device_name[:100],
            platform=body.platform[:20],
            registered_at=datetime.now(timezone.utc),
        )
        db.add(device)
        await db.flush()
        db.add(DeviceToken(device_id=device.id, token_hash=token_hash))
        await db.commit()
        device_id = str(device.id)

    return {"device_id": device_id, "token": raw_token}


@router.get("/devices")
async def list_devices(profile_id: str = Depends(require_profile)):
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Device).where(Device.profile_id == profile_id)
        )).scalars().all()
    return {"devices": [
        {
            "id":        str(d.id),
            "name":      d.name,
            "platform":  d.platform,
            "last_seen": d.last_seen.isoformat() if d.last_seen else None,
        }
        for d in rows
    ]}


@router.delete("/devices/{device_id}")
async def revoke_device(device_id: str, profile_id: str = Depends(require_profile)):
    try:
        did = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(404, "Not found")
    async with AsyncSessionLocal() as db:
        device = await db.get(Device, did)
        if not device or str(device.profile_id) != profile_id:
            raise HTTPException(404, "Not found")
        await db.delete(device)
        await db.commit()
    return {"ok": True}


@router.get("/status")
@limiter.limit("120/minute")
async def agent_status(request: Request):
    """Lightweight check used by tray icon to verify connectivity."""
    from ._base import _get_device
    device, _ = await _get_device(request)
    async with AsyncSessionLocal() as db:
        d = await db.get(Device, device.id)
        if d:
            d.last_seen = datetime.now(timezone.utc)
            await db.commit()
    return {"ok": True, "device": device.name}
