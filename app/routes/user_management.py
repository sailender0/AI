"""User Management console API (the /user-management page).

Elevated-only (manager + admin): lists users (row-scoped), and handles role,
manager assignment, and permission edits. Role changes, manager assignment and the
per-manager delegation allow-list are admin-only; a manager may edit permissions of
their own reports within their assignable set. Every change is audited via _audit().
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import (
    ELEVATED, ROLES, assignable_permissions, can_edit_permissions, granted,
    load_profile, require_elevated, visible_profiles,
)
from app.storage.models import ALL_PERMISSIONS, Profile
from app.storage.mongodb import access_log, purge_profile
from app.storage.postgres import get_db
from app.storage.redis_client import get_redis

router = APIRouter()
logger = logging.getLogger(__name__)


def _own_perms(p: Profile) -> list[str]:
    """The profile's stored permission list, canonicalised. For a manager this is
    the TEAM TEMPLATE (not their own gate — they're elevated and hold all)."""
    return [k for k in ALL_PERMISSIONS if k in (p.permissions or [])]


@router.get("/api/user-management/users")
async def list_users(actor: Profile = Depends(require_elevated(*ELEVATED)),
                     db: AsyncSession = Depends(get_db)):
    """The console list, row-scoped: admin sees everyone, a manager sees only
    themselves + their direct reports. Explicit field list — never dumps a Profile
    wholesale (would leak teams_user_id etc.).

    `permissions` is the effective set (badge); `own_permissions` is the editable
    toggle state; `can_edit_perms` says whether THIS actor may edit THIS row."""
    rows = await visible_profiles(actor, db)
    is_admin = actor.role == "admin"
    managers = [{"id": str(p.id), "email": p.email} for p in rows if p.role == "manager"]
    return JSONResponse({
        "is_admin": is_admin,
        "can_edit": is_admin,
        "actor_id": str(actor.id),
        "roles": list(ROLES),
        "all_permissions": list(ALL_PERMISSIONS),
        "assignable": assignable_permissions(actor),
        "managers": managers,
        "users": [
            {"id": str(p.id), "email": p.email, "role": p.role,
             "permissions": granted(p),
             "own_permissions": _own_perms(p),
             "assignable_perms": [k for k in ALL_PERMISSIONS if k in (p.assignable_perms or [])],
             "manager_id": str(p.manager_id) if p.manager_id else None,
             "can_edit_perms": can_edit_permissions(actor, p)}
            for p in rows
        ],
    })


class RoleBody(BaseModel):
    role: str


class PermsBody(BaseModel):
    permissions: list[str]


class ManagerBody(BaseModel):
    manager_id: str | None = None


class BulkPermsBody(BaseModel):
    permissions: list[str]
    mode: str = "grant"


class BulkAssignBody(BaseModel):
    user_ids: list[str]
    permissions: list[str]
    mode: str = "grant"


async def _get_target(user_id: str, db: AsyncSession) -> Profile | None:
    try:
        return await db.get(Profile, uuid.UUID(user_id))
    except ValueError:
        return None


async def _audit(actor: Profile, target: Profile, action: str, extra: dict | None = None) -> None:
    """Record an access-control change (permission edit, manager assignment, bulk
    apply). Never fails the request on an audit-write error — but logs it loudly."""
    try:
        await access_log().insert_one({
            "actor_profile_id":  str(actor.id),
            "actor_email":       actor.email,
            "actor_role":        actor.role,
            "target_profile_id": str(target.id),
            "target_email":      target.email,
            "action":            action,
            "at":                datetime.now(timezone.utc),
            **(extra or {}),
        })
    except Exception as exc:
        logger.error("access_log write failed (%s of %s by %s): %s",
                     action, target.id, actor.id, exc)


@router.patch("/api/user-management/users/{user_id}/role")
async def set_role(user_id: str, body: RoleBody,
                   actor: Profile = Depends(require_elevated("admin")),
                   db: AsyncSession = Depends(get_db)):
    if body.role not in ROLES:
        return JSONResponse({"error": f"bad role: {body.role}"}, status_code=400)
    if user_id == str(actor.id) and body.role != "admin":
        return JSONResponse({"error": "cannot demote yourself"}, status_code=400)
    target = await _get_target(user_id, db)
    if not target:
        return JSONResponse({"error": "no_such_user"}, status_code=404)
    if target.role == "manager" and body.role != "manager":
        await db.execute(
            update(Profile).where(Profile.manager_id == target.id).values(manager_id=None)
        )
    target.role = body.role
    await db.commit()
    return JSONResponse({"ok": True, "role": target.role})


@router.patch("/api/user-management/users/{user_id}/permissions")
async def set_permissions(user_id: str, body: PermsBody,
                          actor: Profile = Depends(load_profile),
                          db: AsyncSession = Depends(get_db)):
    """Edit a user's own permission list. Admin → anyone; manager → their direct
    reports only (the server clamp, not the UI). For a manager row this list is the
    team template (they're elevated and hold all regardless)."""
    unknown = [p for p in body.permissions if p not in ALL_PERMISSIONS]
    if unknown:
        return JSONResponse({"error": f"unknown permissions: {unknown}"}, status_code=400)
    target = await _get_target(user_id, db)
    if not target:
        return JSONResponse({"error": "no_such_user"}, status_code=404)
    if not can_edit_permissions(actor, target):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    submitted = set(body.permissions)
    if actor.role != "admin":
        allowed, current = set(assignable_permissions(actor)), set(target.permissions or [])
        submitted = (submitted & allowed) | (current - allowed)
    target.permissions = [p for p in ALL_PERMISSIONS if p in submitted]
    await db.commit()
    if str(target.id) != str(actor.id):
        await _audit(actor, target, "set_permissions", {"permissions": target.permissions})
    return JSONResponse({"ok": True, "permissions": target.permissions})


@router.patch("/api/user-management/users/{user_id}/manager")
async def set_manager(user_id: str, body: ManagerBody,
                      actor: Profile = Depends(require_elevated("admin")),
                      db: AsyncSession = Depends(get_db)):
    """Assign (or clear) a user's manager — admin only. On assignment we copy the
    manager's team template onto the user (grant/union), so a new report starts with
    the team's permissions; the admin/manager can then fine-tune per user."""
    target = await _get_target(user_id, db)
    if not target:
        return JSONResponse({"error": "no_such_user"}, status_code=404)

    if not body.manager_id:
        target.manager_id = None
        await db.commit()
        await _audit(actor, target, "assign_manager", {"manager_id": None})
        return JSONResponse({"ok": True, "manager_id": None, "permissions": _own_perms(target)})

    if body.manager_id == user_id:
        return JSONResponse({"error": "cannot report to self"}, status_code=400)
    mgr = await _get_target(body.manager_id, db)
    if not mgr:
        return JSONResponse({"error": "no_such_manager"}, status_code=404)
    if mgr.role != "manager":
        return JSONResponse({"error": "target is not a manager"}, status_code=400)
    if str(mgr.manager_id or "") == user_id:
        return JSONResponse({"error": "would create a manager cycle"}, status_code=400)

    target.manager_id = mgr.id
    merged = set(_own_perms(target)) | set(_own_perms(mgr))
    target.permissions = [p for p in ALL_PERMISSIONS if p in merged]
    await db.commit()
    await _audit(actor, target, "assign_manager",
                 {"manager_id": str(mgr.id), "permissions": target.permissions})
    return JSONResponse({"ok": True, "manager_id": str(mgr.id), "permissions": target.permissions})


@router.patch("/api/user-management/managers/{manager_id}/assignable")
async def set_assignable(manager_id: str, body: PermsBody,
                         actor: Profile = Depends(require_elevated("admin")),
                         db: AsyncSession = Depends(get_db)):
    """Set which permissions a manager may assign to their reports (the Manager
    permissions allow-list). Admin only. Independent of the manager's own permissions."""
    unknown = [p for p in body.permissions if p not in ALL_PERMISSIONS]
    if unknown:
        return JSONResponse({"error": f"unknown permissions: {unknown}"}, status_code=400)
    mgr = await _get_target(manager_id, db)
    if not mgr:
        return JSONResponse({"error": "no_such_manager"}, status_code=404)
    if mgr.role != "manager":
        return JSONResponse({"error": "target is not a manager"}, status_code=400)
    mgr.assignable_perms = [p for p in ALL_PERMISSIONS if p in body.permissions]
    await db.commit()
    await _audit(actor, mgr, "set_assignable", {"assignable": mgr.assignable_perms})
    return JSONResponse({"ok": True, "assignable_perms": mgr.assignable_perms})


@router.post("/api/user-management/managers/{manager_id}/team-permissions")
async def bulk_team_permissions(manager_id: str, body: BulkPermsBody,
                                actor: Profile = Depends(require_elevated("admin")),
                                db: AsyncSession = Depends(get_db)):
    """Grant (add) or revoke (remove) a permission set across ALL of a manager's
    direct reports in one shot. Grant is non-destructive — it keeps each report's
    existing extras; revoke strips only the named keys. Admin only, audited once."""
    if body.mode not in ("grant", "revoke"):
        return JSONResponse({"error": f"bad mode: {body.mode}"}, status_code=400)
    unknown = [p for p in body.permissions if p not in ALL_PERMISSIONS]
    if unknown:
        return JSONResponse({"error": f"unknown permissions: {unknown}"}, status_code=400)

    mgr = await _get_target(manager_id, db)
    if not mgr:
        return JSONResponse({"error": "no_such_manager"}, status_code=404)
    if mgr.role != "manager":
        return JSONResponse({"error": "target is not a manager"}, status_code=400)

    delta = {p for p in body.permissions if p in ALL_PERMISSIONS}
    reports = (await db.execute(
        select(Profile).where(Profile.manager_id == mgr.id)
    )).scalars().all()
    for r in reports:
        cur = set(_own_perms(r))
        cur = (cur | delta) if body.mode == "grant" else (cur - delta)
        r.permissions = [p for p in ALL_PERMISSIONS if p in cur]
    await db.commit()
    await _audit(actor, mgr, f"team_{body.mode}",
                 {"permissions": sorted(delta), "count": len(reports)})
    return JSONResponse({"ok": True, "mode": body.mode, "count": len(reports)})


@router.post("/api/user-management/bulk-permissions")
async def bulk_permissions(body: BulkAssignBody,
                           actor: Profile = Depends(require_elevated(*ELEVATED)),
                           db: AsyncSession = Depends(get_db)):
    """Grant/revoke a permission set across SELECTED users at once. Each target is
    authorised individually (admin → anyone, manager → their reports), so a manager
    can only touch their own team even if they pass other ids. Used by the console's
    'select permissions + select users + assign' panel."""
    if body.mode not in ("grant", "revoke"):
        return JSONResponse({"error": f"bad mode: {body.mode}"}, status_code=400)
    unknown = [p for p in body.permissions if p not in ALL_PERMISSIONS]
    if unknown:
        return JSONResponse({"error": f"unknown permissions: {unknown}"}, status_code=400)

    delta = {p for p in body.permissions if p in ALL_PERMISSIONS}
    if actor.role != "admin":
        delta &= set(assignable_permissions(actor))
    if not delta:
        return JSONResponse({"ok": True, "mode": body.mode, "changed": 0,
                             "skipped": len(body.user_ids)})
    changed, skipped = 0, 0
    for uid in body.user_ids:
        target = await _get_target(uid, db)
        if not target or not can_edit_permissions(actor, target):
            skipped += 1
            continue
        cur = set(_own_perms(target))
        cur = (cur | delta) if body.mode == "grant" else (cur - delta)
        target.permissions = [p for p in ALL_PERMISSIONS if p in cur]
        changed += 1
    await db.commit()
    return JSONResponse({"ok": True, "mode": body.mode, "changed": changed, "skipped": skipped})


@router.delete("/api/user-management/users/{user_id}")
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
    await db.delete(target)
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
            "actor_role":        actor.role,
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


@router.get("/api/user-management/access-log")
async def access_log_view(role: str = "", action: str = "",
                          actor: Profile = Depends(require_elevated("admin")),
                          db: AsyncSession = Depends(get_db)):
    """The cross-user audit trail: who viewed / downloaded / sent / deleted whom.
    Filterable by the actor's role and by action. Profile ids are resolved to
    emails (older rows lack the stored email; deleted users fall back to a short id)."""
    query: dict = {}
    if role:
        query["actor_role"] = role
    if action:
        query["action"] = action

    docs = await access_log().find(query, {"_id": 0}).sort("at", -1).limit(200).to_list(200)

    wanted = set()
    for d in docs:
        for k in ("actor_profile_id", "target_profile_id", "report_owner_id", "recipient_profile_id"):
            if d.get(k):
                wanted.add(d[k])
    emails: dict[str, str] = {}
    valid = []
    for i in wanted:
        try:
            valid.append(uuid.UUID(i))
        except ValueError:
            pass
    if valid:
        rows = (await db.execute(select(Profile.id, Profile.email).where(Profile.id.in_(valid)))).all()
        emails = {str(pid): em for pid, em in rows}

    def resolve(pid, stored=None):
        if stored:
            return stored
        if not pid:
            return None
        return emails.get(pid) or (pid[:8] + "…")

    entries = []
    for d in docs:
        owner_id = d.get("target_profile_id") or d.get("report_owner_id")
        at = d.get("at")
        at_iso = at.replace(tzinfo=timezone.utc).isoformat() if at else None
        entries.append({
            "at":          at_iso,
            "actor_email": resolve(d.get("actor_profile_id"), d.get("actor_email")),
            "actor_role":  d.get("actor_role") or "",
            "action":      d.get("action"),
            "kind":        d.get("kind"),
            "target_email":    resolve(owner_id, d.get("target_email")),
            "recipient_email": resolve(d.get("recipient_profile_id"), d.get("recipient_email")),
        })
    return JSONResponse({"entries": entries})
