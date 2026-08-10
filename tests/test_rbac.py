"""Authorization decisions for report access — the security path in app/auth/rbac.
authorize_report is pure (no DB), so we test the whole truth table here."""
import pytest
from fastapi import HTTPException

from app.auth.rbac import (
    assignable_permissions, authorize_report, can_edit_permissions, granted,
    sanitize_permissions,
)
from app.storage.models import ADMIN_ONLY_PERMISSIONS, ALL_PERMISSIONS, REPORT_PERMISSIONS

ACTOR = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _auth(role, perms, target):
    return authorize_report("export_my_day", role, perms, ACTOR, target)


def test_self_with_permission_is_allowed_and_not_cross_user():
    assert _auth("user", ["export_my_day"], None) is False
    assert _auth("user", ["export_my_day"], ACTOR) is False


def test_self_without_permission_is_forbidden():
    with pytest.raises(HTTPException) as e:
        _auth("user", [], None)
    assert e.value.status_code == 403


def test_admin_self_bypasses_permission():
    assert _auth("admin", [], None) is False


def test_manager_self_needs_permission():
    with pytest.raises(HTTPException) as e:
        _auth("manager", [], None)
    assert e.value.status_code == 403
    assert _auth("manager", ["export_my_day"], None) is False


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
    assert granted(_P("manager", ["export_my_day"])) == ["export_my_day"]
    assert granted(_P("manager", [])) == []


def test_assignable_is_the_managers_admin_set_allowlist():
    mgr = _P("manager", ["attendance_report"], assignable=["export_my_day", "export_analytics"])
    assert assignable_permissions(mgr) == ["export_my_day", "export_analytics"]
    assert "attendance_report" not in assignable_permissions(mgr)
    assert assignable_permissions(_P("manager", ["email_report"])) == []
    assert assignable_permissions(_P("admin", [])) == list(ALL_PERMISSIONS)
    assert assignable_permissions(_P("user", [])) == []


def test_granted_user_filters_to_known_permissions():
    assert granted(_P("user", ["export_my_day", "bogus"])) == ["export_my_day"]

    assert _auth("manager", [], OTHER) is True


def test_can_edit_permissions_admin_edits_anyone():
    admin = _P("admin", [], id="a")
    assert can_edit_permissions(admin, _P("user", [], id="u", manager_id="someone")) is True


def test_manager_edits_only_direct_reports():
    mgr = _P("manager", [], id="m")
    report = _P("user", [], id="u", manager_id="m")
    stranger = _P("user", [], id="v", manager_id="other")
    assert can_edit_permissions(mgr, report) is True
    assert can_edit_permissions(mgr, stranger) is False
    assert can_edit_permissions(mgr, mgr) is False


def test_plain_user_cannot_edit_permissions():
    user = _P("user", [], id="u")
    assert can_edit_permissions(user, _P("user", [], id="v", manager_id="u")) is False


# ── the Report group: admin-only, elevated-only ────────────────────────────────

def test_admin_only_keys_need_an_elevated_role_to_count():
    """A stale key on a demoted account must not grant anything — this is what lets
    demotion skip a cleanup pass over the permissions column."""
    assert granted(_P("manager", ["teams_activity"])) == ["teams_activity"]
    assert granted(_P("user", ["teams_activity"])) == []
    assert granted(_P("user", ["export_my_day", "activity_detail"])) == ["export_my_day"]


def test_manager_can_never_delegate_an_admin_only_key():
    """Even if an admin drops one into the allow-list by mistake."""
    mgr = _P("manager", ADMIN_ONLY_PERMISSIONS,
             assignable=["export_my_day", "teams_activity", "activity_detail"])
    assert assignable_permissions(mgr) == ["export_my_day"]
    # ...but an admin still can.
    for key in ADMIN_ONLY_PERMISSIONS:
        assert key in assignable_permissions(_P("admin", []))


def test_manager_can_never_delegate_a_report_key():
    """The report permissions are holdable by a user but handed out by admins only,
    so they never survive into a manager's assignable set."""
    mgr = _P("manager", REPORT_PERMISSIONS,
             assignable=["export_my_day", "consolidated_report", "attendance_report"])
    assert assignable_permissions(mgr) == ["export_my_day"]
    for key in REPORT_PERMISSIONS:
        assert key in assignable_permissions(_P("admin", []))
        # ...and a user may still HOLD one; only delegation is restricted.
        assert sanitize_permissions([key], _P("user", [])) == [key]


def test_sanitize_strips_admin_only_keys_from_a_plain_user():
    keys = ["export_my_day", "teams_activity", "outlook_activity", "device_activity"]
    assert sanitize_permissions(keys, _P("user", [])) == ["export_my_day"]
    # An elevated target keeps them, in canonical order.
    kept = sanitize_permissions(keys, _P("manager", []))
    assert kept == [p for p in ALL_PERMISSIONS if p in set(keys)]


def test_sanitize_drops_unknown_keys():
    assert sanitize_permissions(["bogus", "export_my_day"], _P("manager", [])) == ["export_my_day"]
