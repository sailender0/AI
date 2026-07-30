"""Roles and permissions — the one place authorization is decided.

Roles (rank order):
  user    — own data only; each feature gated by an admin-toggleable permission.
  manager — plus: list/report/edit-permissions for their DIRECT REPORTS only.
  admin   — plus: any user, change roles, assign managers.

Permissions (models.ALL_PERMISSIONS) gate a profile's access to their OWN features.
Only **admin** implicitly holds all of them. A **manager** is gated by their own
permission list exactly like a user — an admin can restrict what a manager may do —
but managers keep their TEAM powers (list/report/edit their reports) through ROLE
checks (require_elevated, the cross-user branch of authorize_report), which don't
depend on the permission list. A manager's permission list is also the template
copied onto reports at assignment (see admin routes).

Manager scope is DIRECT REPORTS ONLY (Profile.manager_id == manager.id) — no
recursion. A manager sees nobody until an admin assigns reports to them.

Every report route calls report_target(), which answers both questions at once —
"may I run this report?" and "for whom?" — so the permission check and the
cross-user audit can't drift apart.
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import require_profile
from app.storage.models import ALL_PERMISSIONS, Profile
from app.storage.mongodb import access_log
from app.storage.postgres import get_db

logger = logging.getLogger(__name__)

ROLES = ("user", "manager", "admin")
ELEVATED = ("manager", "admin")


async def load_profile(profile_id: str = Depends(require_profile),
                       db: AsyncSession = Depends(get_db)) -> Profile:
    """The signed-in Profile. Routes needing role/permissions take this instead
    of require_profile (which yields only the id)."""
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(401, "not_authenticated")
    return profile


def granted(profile: Profile) -> list[str]:
    """Permissions this profile effectively holds. Only admin implicitly holds all;
    a manager is gated by their own list (so an admin can restrict them), same as a
    user. Managers keep their team powers via role checks, not via this list."""
    if profile.role == "admin":
        return list(ALL_PERMISSIONS)
    return [p for p in (profile.permissions or []) if p in ALL_PERMISSIONS]


def require_permission(key: str):
    """Dependency: 403 unless the caller holds `key`. Yields the caller's id."""
    async def dep(profile: Profile = Depends(load_profile)) -> str:
        if key not in granted(profile):
            raise HTTPException(403, "forbidden")
        return str(profile.id)
    return dep


def require_elevated(*roles: str):
    """Dependency: 403 unless the caller's role is one of `roles`."""
    async def dep(profile: Profile = Depends(load_profile)) -> Profile:
        if profile.role not in roles:
            raise HTTPException(403, "forbidden")
        return profile
    return dep


def assignable_permissions(actor: Profile) -> list[str]:
    """Permissions `actor` may grant/revoke to OTHER users (canonical order). Admin →
    all. Manager → their admin-configured `assignable_perms` allow-list (independent of
    what they hold themselves). User → none. The single source of truth used by both
    the write clamps and the console UI, so they can't drift."""
    if actor.role == "admin":
        return list(ALL_PERMISSIONS)
    if actor.role == "manager":
        allowed = set(actor.assignable_perms or [])
        return [p for p in ALL_PERMISSIONS if p in allowed]
    return []


def can_edit_permissions(actor: Profile, target: Profile) -> bool:
    """May `actor` edit `target`'s permissions? Admin → anyone. Manager → their
    direct reports only (never themselves or another manager who isn't their
    report). Pure so the truth table is testable without a DB."""
    if actor.role == "admin":
        return True
    if actor.role == "manager":
        return str(target.manager_id or "") == str(actor.id)
    return False


async def visible_profiles(actor: Profile, db: AsyncSession) -> list[Profile]:
    """The rows `actor` may see in a multi-user report / console list. Admin → all;
    manager → themselves + their direct reports; user → just themselves. The single
    source of truth for row-scope, so the report grid and the console list agree."""
    if actor.role == "admin":
        return list((await db.execute(select(Profile).order_by(Profile.email))).scalars().all())
    if actor.role == "manager":
        reports = (await db.execute(
            select(Profile).where(Profile.manager_id == actor.id).order_by(Profile.email)
        )).scalars().all()
        return [actor, *reports]
    return [actor]


def authorize_report(permission: str, actor_role: str, actor_permissions: list[str],
                     actor_id: str, target_id: str | None) -> bool:
    """Decide a report request. Returns True when it's cross-user, False for self.
    Raises 403 otherwise. Pure — the DB/audit work lives in report_target().
    """
    if not target_id or target_id == actor_id:
        # Own data: admin always; anyone else needs the permission (a manager's own
        # access is now restrictable — team powers live in the cross-user branch).
        if actor_role == "admin" or permission in actor_permissions:
            return False
        raise HTTPException(403, "forbidden")
    if actor_role not in ELEVATED:
        raise HTTPException(403, "forbidden")
    return True


async def report_target(permission: str, kind: str, user_id: str | None,
                        actor: Profile, db: AsyncSession, action: str = "download",
                        audit: bool = True) -> str:
    """The profile a report should be built for, authorized and audited.

    Self  → needs `permission`. Someone else → needs supervisor/admin, and the
    access is written to access_log before the data is read. Pass audit=False when
    the caller logs its own richer row (e.g. a send that also records the recipient).
    """
    actor_id = str(actor.id)
    if not authorize_report(permission, actor.role, list(actor.permissions or []), actor_id, user_id):
        return actor_id

    try:
        target_uuid = uuid.UUID(str(user_id))
    except ValueError:
        raise HTTPException(404, "no_such_user")
    # 404 rather than 403: the actor IS allowed to read users, this one isn't real.
    target = await db.get(Profile, target_uuid)
    if not target:
        raise HTTPException(404, "no_such_user")

    # A manager is scoped to their direct reports; admin reaches anyone. This is the
    # server-side clamp — the client can pass any user_id, but a non-report is 403.
    if actor.role != "admin" and str(target.manager_id or "") != actor_id:
        raise HTTPException(403, "forbidden")

    if not audit:
        return str(target_uuid)

    try:
        await access_log().insert_one({
            "actor_profile_id":  actor_id,
            "actor_email":       actor.email,
            "actor_role":        actor.role,
            "target_profile_id": str(target_uuid),
            "target_email":      target.email,
            "kind":              kind,
            "action":            action,
            "at":                datetime.now(timezone.utc),
        })
    except Exception as exc:
        # Never fail the request on an audit write, but make the gap loud.
        logger.error("access_log write failed (%s %s of %s by %s): %s",
                     action, kind, target_uuid, actor_id, exc)
    return str(target_uuid)
