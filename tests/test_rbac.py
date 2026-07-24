"""Authorization decisions for report access — the security path in app/auth/rbac.
authorize_report is pure (no DB), so we test the whole truth table here."""
import pytest
from fastapi import HTTPException

from app.auth.rbac import authorize_report, granted
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


def test_elevated_self_bypasses_permission():
    assert _auth("supervisor", [], None) is False
    assert _auth("admin", [], None) is False


def test_cross_user_requires_elevated():
    with pytest.raises(HTTPException) as e:
        _auth("user", ["export_my_day"], OTHER)
    assert e.value.status_code == 403


def test_cross_user_allowed_for_elevated():
    assert _auth("supervisor", [], OTHER) is True
    assert _auth("admin", [], OTHER) is True


class _P:
    def __init__(self, role, perms):
        self.role, self.permissions = role, perms


def test_granted_elevated_holds_all():
    assert granted(_P("supervisor", [])) == list(ALL_PERMISSIONS)
    assert granted(_P("admin", [])) == list(ALL_PERMISSIONS)


def test_granted_user_filters_to_known_permissions():
    assert granted(_P("user", ["export_my_day", "bogus"])) == ["export_my_day"]
