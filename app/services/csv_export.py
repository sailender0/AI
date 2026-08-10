"""Shared CSV-output helpers.

`csv_safe` lived twice — once in services/attendance.py and once in
routes/exports.py — as byte-identical copies. A formula-injection guard with two
homes is one that can be hardened in one place and silently left stale in the
other, so it has one home now. Sits in services because routes may import
services and not the reverse (tests/test_layering.py).
"""

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(v) -> str:
    """One CSV cell, neutralised against spreadsheet formula injection."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in _FORMULA_LEADERS else s
