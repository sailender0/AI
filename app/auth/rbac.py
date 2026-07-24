"""Roles and permissions — the one place authorization is decided.

Roles (rank order):
  user       — own data only; each feature gated by an admin-toggleable permission.
  supervisor — plus: list users, download/email any user's my_day + analytics.
  admin      — plus: change roles and permissions.

Permissions (models.ALL_PERMISSIONS) gate a user's access to their OWN features.
supervisor/admin implicitly hold all of them — a supervisor can't be locked out
of the reports they exist to review, and it keeps the toggles meaning one thing:
"what this regular user may do".

Every report route calls report_target(), which answers both questions at once —
"may I run this report?" and "for whom?" — so the permission check and the
cross-user audit can't drift apart.
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sso import require_profile
from app.storage.models import ALL_PERMISSIONS, Profile
from app.storage.mongodb import access_log
from app.storage.postgres import get_db

logger = logging.getLogger(__name__)

ROLES = ("user", "supervisor", "admin")
ELEVATED = ("supervisor", "admin")


async def load_profile(profile_id: str = Depends(require_profile),
                       db: AsyncSession = Depends(get_db)) -> Profile:
    """The signed-in Profile. Routes needing role/permissions take this instead
    of require_profile (which yields only the id)."""
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(401, "not_authenticated")
    return profile


def granted(profile: Profile) -> list[str]:
    """Permissions this profile effectively holds."""
    if profile.role in ELEVATED:
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


def authorize_report(permission: str, actor_role: str, actor_permissions: list[str],
                     actor_id: str, target_id: str | None) -> bool:
    """Decide a report request. Returns True when it's cross-user, False for self.
    Raises 403 otherwise. Pure — the DB/audit work lives in report_target().
    """
    if not target_id or target_id == actor_id:
        if actor_role in ELEVATED or permission in actor_permissions:
            return False
        raise HTTPException(403, "forbidden")
    if actor_role not in ELEVATED:
        raise HTTPException(403, "forbidden")
    return True


async def report_target(permission: str, kind: str, user_id: str | None,
                        actor: Profile, db: AsyncSession, action: str = "download") -> str:
    """The profile a report should be built for, authorized and audited.

    Self  → needs `permission`. Someone else → needs supervisor/admin, and the
    access is written to access_log before the data is read.
    """
    actor_id = str(actor.id)
    if not authorize_report(permission, actor.role, list(actor.permissions or []), actor_id, user_id):
        return actor_id

    try:
        target_uuid = uuid.UUID(str(user_id))
    except ValueError:
        raise HTTPException(404, "no_such_user")
    # 404 rather than 403: the actor IS allowed to read users, this one isn't real.
    if not await db.get(Profile, target_uuid):
        raise HTTPException(404, "no_such_user")

    try:
        await access_log().insert_one({
            "actor_profile_id":  actor_id,
            "actor_email":       actor.email,
            "actor_role":        actor.role,
            "target_profile_id": str(target_uuid),
            "kind":              kind,
            "action":            action,
            "at":                datetime.now(timezone.utc),
        })
    except Exception as exc:
        # Never fail the request on an audit write, but make the gap loud.
        logger.error("access_log write failed (%s %s of %s by %s): %s",
                     action, kind, target_uuid, actor_id, exc)
    return str(target_uuid)
