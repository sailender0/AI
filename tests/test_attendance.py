"""Attendance report — the grid math (zero-fill, >=3 present, days-present),
range guards, CSV shape, and the row-scope/permission clamp."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.attendance import PRESENT_THRESHOLD, attendance_csv, build_attendance
from app.routes.reports import _attendance_scope

A = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
B = uuid.UUID("00000000-0000-0000-0000-0000000000b0")


def _p(pid, role="user", email=None):
    return SimpleNamespace(id=pid, role=role, email=email or f"{pid}@x.com",
                           manager_id=None, permissions=[], timezone="UTC")


class _AsyncIter:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for i in self._items:
                yield i
        return gen()


def _mock_events(docs):
    col = MagicMock()
    col.aggregate.return_value = _AsyncIter(docs)
    return patch("app.services.attendance.activity_events", return_value=col)


async def _build(profiles, start, end, docs, sources=None):
    with _mock_events(docs):
        return await build_attendance(profiles, start, end, sources or [], "UTC")


async def test_zero_filled_absent_user_still_a_row():
    data = await _build([_p(A), _p(B)], "2026-07-20", "2026-07-22", docs=[
        {"_id": {"p": str(A), "d": "2026-07-20"}, "n": 5},
    ])
    assert len(data["days"]) == 3
    a, b = data["rows"]
    assert a["counts"] == [5, 0, 0]
    assert b["counts"] == [0, 0, 0]
    assert b["present"] == 0 and b["total"] == 0


async def test_present_threshold_is_ge_three():
    data = await _build([_p(A)], "2026-07-20", "2026-07-23", docs=[
        {"_id": {"p": str(A), "d": "2026-07-20"}, "n": 2},
        {"_id": {"p": str(A), "d": "2026-07-21"}, "n": 3},
        {"_id": {"p": str(A), "d": "2026-07-22"}, "n": 9},
    ])
    assert PRESENT_THRESHOLD == 3
    row = data["rows"][0]
    assert row["counts"] == [2, 3, 9, 0]
    assert row["present"] == 2
    assert row["total"] == 14


async def test_days_present_counts_all_calendar_days():
    docs = [{"_id": {"p": str(A), "d": f"2026-07-{20+i:02d}"}, "n": 4} for i in range(7)]
    data = await _build([_p(A)], "2026-07-20", "2026-07-26", docs=docs)
    assert data["rows"][0]["present"] == 7


async def test_range_too_large_rejected():
    with pytest.raises(ValueError):
        await _build([_p(A)], "2025-01-01", "2026-12-31", docs=[])


async def test_end_before_start_rejected():
    with pytest.raises(ValueError):
        await _build([_p(A)], "2026-07-22", "2026-07-20", docs=[])


async def test_bad_date_rejected():
    with pytest.raises(ValueError):
        await _build([_p(A)], "not-a-date", "2026-07-20", docs=[])


async def test_csv_is_p_a_marks_no_total_and_text_dates():
    data = await _build([_p(A, email="dev@x.com")], "2026-07-20", "2026-07-21", docs=[
        {"_id": {"p": str(A), "d": "2026-07-20"}, "n": 4},
    ])
    csv_text = attendance_csv(data)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("User,Role,Jul 20")
    assert lines[0].endswith("Days present")
    assert "Total events" not in lines[0]
    assert lines[1].startswith("dev@x.com,user,P,A")
    assert lines[1].endswith(",1")


def test_csv_neutralises_formula_injection():
    data = {"days": ["2026-07-20"], "rows": [
        {"email": "=cmd()", "role": "user", "counts": [1], "present": 0, "total": 1}]}
    assert "'=cmd()" in attendance_csv(data)


async def test_scope_denied_without_permission():
    user = _p(A, "user")
    with pytest.raises(HTTPException) as e:
        await _attendance_scope(user, None, AsyncMock())
    assert e.value.status_code == 403


async def test_user_scope_is_self_only():
    user = _p(A, "user")
    user.permissions = ["attendance_report"]
    scoped = await _attendance_scope(user, None, AsyncMock())
    assert [p.id for p in scoped] == [A]


async def test_selection_clamped_to_scope():
    user = _p(A, "user")
    user.permissions = ["attendance_report"]
    scoped = await _attendance_scope(user, [str(A), str(B)], AsyncMock())
    assert [p.id for p in scoped] == [A]


async def test_admin_scope_is_everyone():
    admin = _p(A, "admin")
    everyone = [admin, _p(B)]
    db = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.all.return_value = everyone
    db.execute = AsyncMock(return_value=res)
    scoped = await _attendance_scope(admin, None, db)
    assert {p.id for p in scoped} == {A, B}
