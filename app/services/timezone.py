"""The single place UTC <-> local conversion happens (see docs/adr-0001-timezone.md).

Rule: storage is UTC, local time is keyed on a per-user IANA timezone, and no
feature computes day boundaries or local dates inline — everything routes here.
Numeric offsets are deliberately absent: they are wrong across DST and for
historical dates.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = timezone.utc


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


def day_bounds(date_str: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC [start, end) covering one local calendar day.

    Anchored on local midnight and the *next* local midnight, so the window is
    23/24/25h across DST transitions — `zoneinfo` resolves the offsets.
    """
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start_local = datetime(d.year, d.month, d.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)  # wall-clock +1 day, then convert
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def local_date(utc_dt: datetime, tz: ZoneInfo) -> str:
    """UTC datetime -> the user's local 'YYYY-MM-DD'. Naive input is assumed UTC
    (Motor returns naive-UTC datetimes by default)."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=_UTC)
    return utc_dt.astimezone(tz).strftime("%Y-%m-%d")
