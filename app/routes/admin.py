"""Admin/supervisor console API.

/admin page (elevated: supervisor + admin) lists users and pulls any user's
my_day / analytics report. Role and per-user permission edits are admin-only.
Downloading/emailing another user's report is authorized + audited in
app/auth/rbac.report_target (via the export and email routes with ?user_id=).
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import ELEVATED, ROLES, granted, require_elevated
from app.storage.models import ALL_PERMISSIONS, Profile
from app.storage.mongodb import access_log, purge_profile
from app.storage.postgres import get_db
from app.storage.redis_client import get_redis

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/admin/users")
async def list_users(actor: Profile = Depends(require_elevated(*ELEVATED)),
                     db: AsyncSession = Depends(get_db)):
    """All users, for the console list. Explicit field list — never dumps a
    Profile wholesale (would leak teams_user_id etc.)."""
    rows = (await db.execute(select(Profile).order_by(Profile.email))).scalars().all()
    can_edit = actor.role == "admin"
    return JSONResponse({
        "can_edit": can_edit,
        "roles": list(ROLES),
        "all_permissions": list(ALL_PERMISSIONS),
        "users": [
            {"id": str(p.id), "email": p.email, "role": p.role,
             "permissions": granted(p)}
            for p in rows
        ],
    })


class RoleBody(BaseModel):
    role: str


class PermsBody(BaseModel):
    permissions: list[str]


async def _get_target(user_id: str, db: AsyncSession) -> Profile | None:
    try:
        return await db.get(Profile, uuid.UUID(user_id))
    except ValueError:
        return None


@router.patch("/api/admin/users/{user_id}/role")
async def set_role(user_id: str, body: RoleBody,
                   actor: Profile = Depends(require_elevated("admin")),
                   db: AsyncSession = Depends(get_db)):
    if body.role not in ROLES:
        return JSONResponse({"error": f"bad role: {body.role}"}, status_code=400)
    if user_id == str(actor.id) and body.role != "admin":
        # ponytail: stops an admin locking themselves out; the env allowlist is
        # the only other recovery path.
        return JSONResponse({"error": "cannot demote yourself"}, status_code=400)
    target = await _get_target(user_id, db)
    if not target:
        return JSONResponse({"error": "no_such_user"}, status_code=404)
    target.role = body.role
    await db.commit()
    return JSONResponse({"ok": True, "role": target.role})


@router.patch("/api/admin/users/{user_id}/permissions")
async def set_permissions(user_id: str, body: PermsBody,
                          actor: Profile = Depends(require_elevated("admin")),
                          db: AsyncSession = Depends(get_db)):
    unknown = [p for p in body.permissions if p not in ALL_PERMISSIONS]
    if unknown:
        return JSONResponse({"error": f"unknown permissions: {unknown}"}, status_code=400)
    target = await _get_target(user_id, db)
    if not target:
        return JSONResponse({"error": "no_such_user"}, status_code=404)
    # Dedup + canonical order; elevated roles ignore this list but store it anyway
    # so a later demotion to 'user' restores exactly what the admin set.
    target.permissions = [p for p in ALL_PERMISSIONS if p in body.permissions]
    await db.commit()
    return JSONResponse({"ok": True, "permissions": target.permissions})


@router.delete("/api/admin/users/{user_id}")
async def delete_user(user_id: str, actor: Profile = Depends(require_elevated("admin")),
                      db: AsyncSession = Depends(get_db)):
    """Permanently remove a user and their data. IRREVERSIBLE.

    Postgres: the ORM cascade takes linked identities, integrations, summaries,
    query logs, chats (+messages), devices (+tokens) and email preferences.
    Mongo: purge_profile clears the activity collections (access_log survives).
    Redis: the cached MSAL token is dropped; any live session 401s on next request
    because load_profile can no longer find the profile.
    """
    if user_id == str(actor.id):
        return JSONResponse({"error": "cannot delete yourself"}, status_code=400)
    target = await _get_target(user_id, db)
    if not target:
        return JSONResponse({"error": "no_such_user"}, status_code=404)

    email = target.email
    await db.delete(target)   # awaited: cascade may load relationships (async ORM)
    await db.commit()

    purged = await purge_profile(user_id)
    try:
        await get_redis().delete(f"msal_cache:{user_id}")
    except Exception as exc:
        logger.error("redis msal_cache purge failed for %s: %s", user_id, exc)

    try:
        await access_log().insert_one({
            "actor_profile_id":  str(actor.id),
            "actor_email":       actor.email,
            "target_profile_id": user_id,
            "target_email":      email,
            "action":            "delete_user",
            "purged":            purged,
            "at":                datetime.now(timezone.utc),
        })
    except Exception as exc:
        logger.error("access_log delete write failed for %s: %s", user_id, exc)

    logger.info("admin %s deleted user %s (%s); mongo purged: %s",
                actor.email, user_id, email, purged)
    return JSONResponse({"ok": True, "deleted": email, "purged": purged})
