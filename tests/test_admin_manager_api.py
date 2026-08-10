"""Admin/manager console endpoints — manager assignment, copy-on-assignment,
bulk team grant/revoke, and the manager permission-edit clamp. DB is mocked
(AsyncMock), so mutations are asserted on the returned namespaces."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.rbac import load_profile
from app.storage.postgres import get_db

ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
MGR   = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
MGR2  = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
REP   = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
STRAY = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def _p(pid, role, manager_id=None, perms=None, assignable=None):
    return SimpleNamespace(id=pid, role=role, manager_id=manager_id,
                           permissions=list(perms or []), assignable_perms=list(assignable or []),
                           email=f"{role}@x.com")


async def _client(actor, db):
    from app.routes.user_management import router
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[load_profile] = lambda: actor
    app.include_router(router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _db(get=None, rows=None):
    db = AsyncMock()
    db.get = AsyncMock(side_effect=get) if isinstance(get, list) else AsyncMock(return_value=get)
    db.commit = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.all.return_value = rows or []
    db.execute = AsyncMock(return_value=res)
    return db


async def test_assign_manager_changes_nothing_but_the_org_tree():
    """Assignment is structure, not access: the report keeps exactly the permissions
    they had, and inherits none of the manager's — not the ones they lack, and not
    the ones the manager holds that they don't."""
    admin = _p(ADMIN, "admin")
    report = _p(REP, "user", perms=["email_report", "attendance_report"])
    mgr = _p(MGR, "manager", perms=["consolidated_report", "export_analytics",
                                    "teams_activity", "activity_detail"])
    db = _db(get=[report, mgr])
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.patch(f"/api/user-management/users/{REP}/manager", json={"manager_id": str(MGR)})
    assert r.status_code == 200
    assert report.manager_id == MGR
    assert set(report.permissions) == {"email_report", "attendance_report"}
    assert r.json()["permissions"] == ["email_report", "attendance_report"]


async def test_admin_cannot_grant_an_admin_only_key_to_a_plain_user():
    admin = _p(ADMIN, "admin")
    report = _p(REP, "user", perms=["email_report"])
    db = _db(get=report)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.patch(f"/api/user-management/users/{REP}/permissions",
                              json={"permissions": ["email_report", "teams_activity"]})
    assert r.status_code == 200
    assert report.permissions == ["email_report"]


async def test_admin_only_keys_are_rejected_from_a_managers_allowlist():
    admin = _p(ADMIN, "admin")
    mgr = _p(MGR, "manager")
    db = _db(get=mgr)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.patch(f"/api/user-management/managers/{MGR}/assignable",
                              json={"permissions": ["export_my_day", "outlook_activity"]})
    assert r.status_code == 200
    assert mgr.assignable_perms == ["export_my_day"]


async def test_team_bulk_grant_skips_admin_only_keys():
    admin = _p(ADMIN, "admin")
    mgr = _p(MGR, "manager")
    reports = [_p(REP, "user"), _p(STRAY, "user")]
    db = _db(get=mgr, rows=reports)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.post(f"/api/user-management/managers/{MGR}/team-permissions",
                             json={"permissions": ["export_my_day", "device_activity"], "mode": "grant"})
    assert r.status_code == 200
    for rep in reports:
        assert rep.permissions == ["export_my_day"]


async def test_cannot_report_to_self():
    admin = _p(ADMIN, "admin")
    db = _db(get=_p(REP, "user"))
    async with await _client(admin, db) as c:
        r = await c.patch(f"/api/user-management/users/{REP}/manager", json={"manager_id": str(REP)})
    assert r.status_code == 400


async def test_manager_target_must_be_a_manager():
    admin = _p(ADMIN, "admin")
    db = _db(get=[_p(REP, "user"), _p(STRAY, "user")])
    async with await _client(admin, db) as c:
        r = await c.patch(f"/api/user-management/users/{REP}/manager", json={"manager_id": str(STRAY)})
    assert r.status_code == 400


async def test_rejects_two_cycle():
    admin = _p(ADMIN, "admin")
    db = _db(get=[_p(REP, "user"), _p(MGR2, "manager", manager_id=REP)])
    async with await _client(admin, db) as c:
        r = await c.patch(f"/api/user-management/users/{REP}/manager", json={"manager_id": str(MGR2)})
    assert r.status_code == 400


async def test_unassign_manager():
    admin = _p(ADMIN, "admin")
    report = _p(REP, "user", manager_id=MGR, perms=["email_report"])
    db = _db(get=report)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.patch(f"/api/user-management/users/{REP}/manager", json={"manager_id": None})
    assert r.status_code == 200
    assert report.manager_id is None


async def test_bulk_grant_is_additive():
    admin = _p(ADMIN, "admin")
    mgr = _p(MGR, "manager")
    r1 = _p(REP, "user", manager_id=MGR, perms=["email_report"])
    r2 = _p(STRAY, "user", manager_id=MGR, perms=[])
    db = _db(get=mgr, rows=[r1, r2])
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.post(f"/api/user-management/managers/{MGR}/team-permissions",
                             json={"permissions": ["consolidated_report"], "mode": "grant"})
    assert r.status_code == 200 and r.json()["count"] == 2
    assert "email_report" in r1.permissions and "consolidated_report" in r1.permissions
    assert r2.permissions == ["consolidated_report"]


async def test_bulk_revoke_removes_only_named():
    admin = _p(ADMIN, "admin")
    mgr = _p(MGR, "manager")
    r1 = _p(REP, "user", manager_id=MGR, perms=["email_report", "consolidated_report"])
    db = _db(get=mgr, rows=[r1])
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.post(f"/api/user-management/managers/{MGR}/team-permissions",
                             json={"permissions": ["consolidated_report"], "mode": "revoke"})
    assert r.status_code == 200
    assert r1.permissions == ["email_report"]


async def test_bulk_assign_selected_users_authorised_per_user():
    mgr = _p(MGR, "manager", assignable=["export_my_day"])
    mine = _p(REP, "user", manager_id=MGR, perms=[])
    not_mine = _p(STRAY, "user", manager_id=MGR2, perms=[])
    db = _db(get=[mine, not_mine])
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(mgr, db) as c:
            r = await c.post("/api/user-management/bulk-permissions",
                             json={"user_ids": [str(REP), str(STRAY)],
                                   "permissions": ["export_my_day"], "mode": "grant"})
    d = r.json()
    assert r.status_code == 200 and d["changed"] == 1 and d["skipped"] == 1
    assert mine.permissions == ["export_my_day"]
    assert not_mine.permissions == []


async def test_bulk_bad_mode_rejected():
    admin = _p(ADMIN, "admin")
    db = _db(get=_p(MGR, "manager"))
    async with await _client(admin, db) as c:
        r = await c.post(f"/api/user-management/managers/{MGR}/team-permissions",
                         json={"permissions": ["consolidated_report"], "mode": "nuke"})
    assert r.status_code == 400


async def test_manager_edits_own_report_permissions():
    mgr = _p(MGR, "manager", assignable=["export_my_day"])
    report = _p(REP, "user", manager_id=MGR, perms=[])
    db = _db(get=report)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(mgr, db) as c:
            r = await c.patch(f"/api/user-management/users/{REP}/permissions",
                              json={"permissions": ["export_my_day"]})
    assert r.status_code == 200
    assert report.permissions == ["export_my_day"]


async def test_manager_cannot_grant_outside_allowlist():
    mgr = _p(MGR, "manager", assignable=["export_my_day"])
    report = _p(REP, "user", manager_id=MGR, perms=[])
    db = _db(get=report)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(mgr, db) as c:
            r = await c.patch(f"/api/user-management/users/{REP}/permissions",
                              json={"permissions": ["export_my_day", "export_analytics"]})
    assert r.status_code == 200
    assert report.permissions == ["export_my_day"]


async def test_manager_cannot_grant_a_report_permission():
    """The report keys are non-delegable: a manager holding one, and with it stale
    in their allow-list, still cannot pass it to a report."""
    mgr = _p(MGR, "manager", perms=["attendance_report"],
             assignable=["export_my_day", "consolidated_report", "attendance_report"])
    report = _p(REP, "user", manager_id=MGR, perms=[])
    db = _db(get=report)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(mgr, db) as c:
            r = await c.patch(f"/api/user-management/users/{REP}/permissions",
                              json={"permissions": ["attendance_report", "consolidated_report",
                                                    "export_my_day"]})
    assert r.status_code == 200
    assert report.permissions == ["export_my_day"]


async def test_admin_can_still_assign_attendance():
    admin = _p(ADMIN, "admin")
    report = _p(REP, "user", manager_id=MGR, perms=[])
    db = _db(get=report)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.patch(f"/api/user-management/users/{REP}/permissions",
                              json={"permissions": ["attendance_report"]})
    assert r.status_code == 200
    assert report.permissions == ["attendance_report"]


async def test_manager_bulk_clamped_to_allowlist():
    mgr = _p(MGR, "manager", assignable=["consolidated_report"])
    report = _p(REP, "user", manager_id=MGR, perms=[])
    db = _db(get=report)
    async with await _client(mgr, db) as c:
        r = await c.post("/api/user-management/bulk-permissions",
                         json={"user_ids": [str(REP)], "permissions": ["attendance_report"], "mode": "grant"})
    assert r.status_code == 200 and r.json()["changed"] == 0


async def test_admin_sets_manager_assignable_list():
    admin = _p(ADMIN, "admin")
    mgr = _p(MGR, "manager", assignable=[])
    db = _db(get=mgr)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.patch(f"/api/user-management/managers/{MGR}/assignable",
                              json={"permissions": ["email_report", "export_analytics"]})
    assert r.status_code == 200
    assert mgr.assignable_perms == ["email_report", "export_analytics"]


async def test_report_keys_are_rejected_from_a_managers_allowlist():
    """Even an admin cannot put the report permissions in a manager's allow-list —
    who sees report data stays an admin decision, per user."""
    admin = _p(ADMIN, "admin")
    mgr = _p(MGR, "manager", assignable=[])
    db = _db(get=mgr)
    with patch("app.routes.user_management.access_log", return_value=MagicMock(insert_one=AsyncMock())):
        async with await _client(admin, db) as c:
            r = await c.patch(f"/api/user-management/managers/{MGR}/assignable",
                              json={"permissions": ["email_report", "consolidated_report",
                                                    "attendance_report"]})
    assert r.status_code == 200
    assert mgr.assignable_perms == ["email_report"]


async def test_set_assignable_rejects_non_manager():
    admin = _p(ADMIN, "admin")
    db = _db(get=_p(REP, "user"))
    async with await _client(admin, db) as c:
        r = await c.patch(f"/api/user-management/managers/{REP}/assignable",
                          json={"permissions": ["email_report"]})
    assert r.status_code == 400


async def test_manager_cannot_edit_non_report():
    mgr = _p(MGR, "manager")
    stray = _p(STRAY, "user", manager_id=MGR2)
    db = _db(get=stray)
    async with await _client(mgr, db) as c:
        r = await c.patch(f"/api/user-management/users/{STRAY}/permissions",
                          json={"permissions": ["consolidated_report"]})
    assert r.status_code == 403


async def test_plain_user_cannot_reach_permission_edit():
    user = _p(REP, "user", manager_id=MGR)
    db = _db(get=_p(STRAY, "user", manager_id=REP))
    async with await _client(user, db) as c:
        r = await c.patch(f"/api/user-management/users/{STRAY}/permissions",
                          json={"permissions": ["consolidated_report"]})
    assert r.status_code == 403
