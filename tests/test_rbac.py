"""Authorization decisions for report access — the security path in app/auth/rbac.
authorize_report is pure (no DB), so we test the whole truth table here."""
import pytest
from fastapi import HTTPException

from app.auth.rbac import assignable_permissions, authorize_report, can_edit_permissions, granted
from app.storage.models import ALL_PERMISSIONS

ACTOR = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _auth(role, perms, target):
    return authorize_report("export_my_day", role, perms, ACTOR, target)


def test_self_with_permission_is_allowed_and_not_cross_user():
    assert _auth("user", ["export_my_day"], None) is False
    assert _auth("user", ["export_my_day"], ACTOR) is False   # explicit self id


def test_self_without_permission_is_forbidden():
    with pytest.raises(HTTPException) as e:
        _auth("user", [], None)
    assert e.value.status_code == 403


def test_admin_self_bypasses_permission():
    assert _auth("admin", [], None) is False


def test_manager_self_needs_permission():
    # B: a manager's OWN access is gated by their own list (admin can restrict them).
    with pytest.raises(HTTPException) as e:
        _auth("manager", [], None)
    assert e.value.status_code == 403
    assert _auth("manager", ["export_my_day"], None) is False   # has it → allowed


def test_cross_user_requires_elevated():
    with pytest.raises(HTTPException) as e:
        _auth("user", ["export_my_day"], OTHER)
    assert e.value.status_code == 403


def test_cross_user_allowed_for_elevated():
    assert _auth("manager", [], OTHER) is True
    assert _auth("admin", [], OTHER) is True


class _P:
    def __init__(self, role, perms, id="me", manager_id=None, assignable=None):
        self.role, self.permissions, self.id, self.manager_id = role, perms, id, manager_id
        self.assignable_perms = assignable or []


def test_granted_admin_holds_all():
    assert granted(_P("admin", [])) == list(ALL_PERMISSIONS)


def test_granted_manager_gated_by_own_list():
    # B: managers are no longer auto-all — they hold exactly their own list.
    assert granted(_P("manager", ["export_my_day"])) == ["export_my_day"]
    assert granted(_P("manager", [])) == []


def test_assignable_is_the_managers_admin_set_allowlist():
    # What a manager HOLDS is irrelevant to what they can ASSIGN — only the
    # admin-configured allow-list matters. Here they hold attendance but can't assign it.
    mgr = _P("manager", ["attendance_report"], assignable=["export_my_day", "consolidated_report"])
    assert assignable_permissions(mgr) == ["export_my_day", "consolidated_report"]
    assert "attendance_report" not in assignable_permissions(mgr)
    assert assignable_permissions(_P("manager", ["email_report"])) == []   # default: assign nothing
    assert assignable_permissions(_P("admin", [])) == list(ALL_PERMISSIONS)
    assert assignable_permissions(_P("user", [])) == []


def test_granted_user_filters_to_known_permissions():
    assert granted(_P("user", ["export_my_day", "bogus"])) == ["export_my_day"]

    # cross-user is still role-based — a manager reaches reports without the permission
    assert _auth("manager", [], OTHER) is True


def test_can_edit_permissions_admin_edits_anyone():
    admin = _P("admin", [], id="a")
    assert can_edit_permissions(admin, _P("user", [], id="u", manager_id="someone")) is True


def test_manager_edits_only_direct_reports():
    mgr = _P("manager", [], id="m")
    report = _P("user", [], id="u", manager_id="m")   # reports to m
    stranger = _P("user", [], id="v", manager_id="other")
    assert can_edit_permissions(mgr, report) is True
    assert can_edit_permissions(mgr, stranger) is False
    assert can_edit_permissions(mgr, mgr) is False       # not their own report


def test_plain_user_cannot_edit_permissions():
    user = _P("user", [], id="u")
    assert can_edit_permissions(user, _P("user", [], id="v", manager_id="u")) is False
