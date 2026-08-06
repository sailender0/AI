"""The single place UTC <-> local conversion happens (see docs/adr-0001-timezone.md).

Rule: storage is UTC, local time is keyed on a per-user IANA timezone, and no
feature computes day boundaries or local dates inline — everything routes here.
Numeric offsets are deliberately absent: they are wrong across DST and for
historical dates.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = timezone.utc

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_date(s: str | None) -> bool:
    """True for a well-formed 'YYYY-MM-DD'. Strict: empty is False.

    This module defines what a date string means here (today_str, day_bounds and
    local_date all speak it), so the check that a user-supplied one is safe to put
    in a Mongo query or a Content-Disposition filename belongs here too. Callers
    that treat empty as "use the default" spell that out themselves — blurring the
    two into one predicate is how a blank date silently became a wildcard.
    """
    return bool(s and _DATE_RE.match(s))


def is_valid_tz(name: str | None) -> bool:
    """True if `name` is a resolvable IANA timezone (used before persisting a
    browser-sent tz to the profile)."""
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def resolve(profile_tz: str | None = None, request_tz: str | None = None) -> ZoneInfo:
    """Pick the user's timezone: request (freshest) > stored profile > UTC.
    Invalid names are skipped rather than raising."""
    for name in (request_tz, profile_tz):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def today_str(tz: ZoneInfo) -> str:
    """The user's current local date, e.g. the anchor for 'today'/'yesterday'."""
    return datetime.now(tz).strftime("%Y-%m-%d")


def day_bounds(date_str: str, tz: ZoneInfo | str | None) -> tuple[datetime, datetime]:
    """UTC [start, end) covering one local calendar day.

    Anchored on local midnight and the *next* local midnight, so the window is
    23/24/25h across DST transitions — `zoneinfo` resolves the offsets.

    `tz` takes an IANA name as well as a ZoneInfo so the Graph connectors, which
    carry the profile's zone around as a string, route here instead of keeping
    their own copy. An unresolvable name falls back to UTC (see resolve).
    """
    if not isinstance(tz, ZoneInfo):
        tz = resolve(tz)
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start_local = datetime(d.year, d.month, d.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def local_date(utc_dt: datetime, tz: ZoneInfo) -> str:
    """UTC datetime -> the user's local 'YYYY-MM-DD'. Naive input is assumed UTC
    (Motor returns naive-UTC datetimes by default)."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=_UTC)
    return utc_dt.astimezone(tz).strftime("%Y-%m-%d")
