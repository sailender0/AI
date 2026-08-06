"""Manager/permission scenarios from every profile's perspective.

Three authorization surfaces are exercised end-to-end for user / manager / admin:
  - report_target()      — who may pull WHOSE report (the cross-user clamp)
  - visible_profiles()   — which rows a role sees in a multi-user report/console
  - can_edit_permissions — who may edit whose permissions

The manager rule under test everywhere: DIRECT REPORTS ONLY. A manager reaches
their reports and nobody else; admin reaches everyone; a plain user only themselves.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.auth.rbac import (
    can_edit_permissions, report_target, visible_profiles,
)

ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
MGR   = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
MGR2  = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
REP   = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
STRAY = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def _p(pid, role, manager_id=None, perms=None, email="x@x.com"):
    return SimpleNamespace(id=pid, role=role, manager_id=manager_id,
                           permissions=perms or [], email=email)


def _db_get(target):
    db = AsyncMock()
    db.get = AsyncMock(return_value=target)
    return db


def _db_scalars(rows):
    db = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=res)
    return db


def _audit():
    col = MagicMock()
    col.insert_one = AsyncMock()
    return col


async def test_admin_pulls_any_users_report():
    admin, target = _p(ADMIN, "admin"), _p(STRAY, "user", manager_id=MGR2)
    with patch("app.auth.rbac.access_log", return_value=_audit()):
        out = await report_target("export_my_day", "my_day", str(STRAY), admin, _db_get(target))
    assert out == str(STRAY)


async def test_manager_pulls_direct_report():
    mgr, report = _p(MGR, "manager"), _p(REP, "user", manager_id=MGR)
    with patch("app.auth.rbac.access_log", return_value=_audit()) as al:
        out = await report_target("export_my_day", "my_day", str(REP), mgr, _db_get(report))
    assert out == str(REP)
    al.return_value.insert_one.assert_awaited_once()


async def test_manager_blocked_from_non_report():
    mgr, stray = _p(MGR, "manager"), _p(STRAY, "user", manager_id=MGR2)
    with patch("app.auth.rbac.access_log", return_value=_audit()):
        with pytest.raises(HTTPException) as e:
            await report_target("export_my_day", "my_day", str(STRAY), mgr, _db_get(stray))
    assert e.value.status_code == 403


async def test_manager_pulls_own_report_is_self_path():
    mgr = _p(MGR, "manager", perms=["export_my_day"])
    db = _db_get(None)
    out = await report_target("export_my_day", "my_day", str(MGR), mgr, db)
    assert out == str(MGR)
    db.get.assert_not_called()


async def test_manager_own_report_denied_when_permission_revoked():
    mgr = _p(MGR, "manager", perms=[])
    with pytest.raises(HTTPException) as e:
        await report_target("export_my_day", "my_day", str(MGR), mgr, _db_get(None))
    assert e.value.status_code == 403


async def test_user_self_with_permission():
    user = _p(REP, "user", manager_id=MGR, perms=["export_my_day"])
    out = await report_target("export_my_day", "my_day", None, user, _db_get(None))
    assert out == str(REP)


async def test_user_self_without_permission_forbidden():
    user = _p(REP, "user", manager_id=MGR, perms=[])
    with pytest.raises(HTTPException) as e:
        await report_target("export_my_day", "my_day", None, user, _db_get(None))
    assert e.value.status_code == 403


async def test_user_cannot_pull_another_users_report():
    user = _p(REP, "user", manager_id=MGR, perms=["export_my_day"])
    with pytest.raises(HTTPException) as e:
        await report_target("export_my_day", "my_day", str(STRAY), user, _db_get(_p(STRAY, "user")))
    assert e.value.status_code == 403


async def test_report_target_unknown_user_is_404():
    admin = _p(ADMIN, "admin")
    with pytest.raises(HTTPException) as e:
        await report_target("export_my_day", "my_day", str(STRAY), admin, _db_get(None))
    assert e.value.status_code == 404


async def test_admin_sees_all_rows():
    admin = _p(ADMIN, "admin")
    everyone = [admin, _p(MGR, "manager"), _p(REP, "user", manager_id=MGR)]
    rows = await visible_profiles(admin, _db_scalars(everyone))
    assert rows == everyone


async def test_manager_sees_self_plus_reports():
    mgr = _p(MGR, "manager")
    reports = [_p(REP, "user", manager_id=MGR)]
    rows = await visible_profiles(mgr, _db_scalars(reports))
    assert rows[0] is mgr
    assert reports[0] in rows
    assert len(rows) == 2


async def test_user_sees_only_self_no_db():
    user = _p(REP, "user", manager_id=MGR)
    db = _db_scalars([])
    rows = await visible_profiles(user, db)
    assert rows == [user]
    db.execute.assert_not_called()


def test_admin_edits_anyone_including_managers():
    admin = _p(ADMIN, "admin")
    assert can_edit_permissions(admin, _p(MGR, "manager")) is True
    assert can_edit_permissions(admin, _p(REP, "user", manager_id=MGR)) is True


def test_manager_edits_direct_report_only():
    mgr = _p(MGR, "manager")
    assert can_edit_permissions(mgr, _p(REP, "user", manager_id=MGR)) is True
    assert can_edit_permissions(mgr, _p(STRAY, "user", manager_id=MGR2)) is False


def test_manager_cannot_edit_self_or_peer_manager():
    mgr = _p(MGR, "manager")
    assert can_edit_permissions(mgr, mgr) is False
    assert can_edit_permissions(mgr, _p(MGR2, "manager")) is False


def test_plain_user_edits_nobody():
    user = _p(REP, "user", manager_id=MGR)
    assert can_edit_permissions(user, user) is False
    assert can_edit_permissions(user, _p(STRAY, "user", manager_id=REP)) is False
